import unittest
import inspect
from pathlib import Path
from unittest.mock import Mock, patch

from werkzeug.datastructures import MultiDict

from serdes_metrics import (
    canonical_ieee_url,
    classify_link_medium,
    extract_performance,
    screen_serdes_relevance,
)
from scripts.serdes_data_pipeline import fetch_abstracts, screen_venue, split_sql_statements
import web.app as app_module


class SerdesSurveyTests(unittest.TestCase):
    def test_link_medium_keeps_optical_separate_from_cmos_technology(self):
        cases = [
            "A 56-Gb/s Optical Receiver in 28-nm CMOS",
            "An Electrical IC for a 3-D Integrated Silicon Photonic Transmitter",
            "A 56-Gb/s De-serializer With PAM-4 CDR for Chiplet Optical-I/O",
            "A 225-mW SerDes for 100GBASE-LR4 Optical Transport",
        ]

        for title in cases:
            with self.subTest(title=title):
                result = classify_link_medium(title)
                self.assertEqual(result["link_medium"], "optical")
                self.assertEqual(result["medium_source"], "title")

    def test_link_medium_recognizes_explicit_and_default_electrical_links(self):
        cases = [
            "A 112-Gb/s PAM-4 SerDes Receiver in 7-nm CMOS",
            "A 0.29-pJ/b UCIe Advanced Package Link",
            "NVLink-C2C: A Coherent Off-Package Chip-to-Chip Interconnect",
            "A Receiver Tested Over a 30-dB PCB Channel",
        ]

        for title in cases:
            with self.subTest(title=title):
                self.assertEqual(
                    classify_link_medium(title)["link_medium"], "electrical",
                )

    def test_link_medium_uses_abstract_only_for_implementation_context(self):
        optical = classify_link_medium(
            "A Generic 56-Gb/s PAM-4 Receiver",
            "This receiver targets a short-reach optical link and is fabricated in CMOS.",
        )
        background_only = classify_link_medium(
            "A 150-Gb/s PAM-4 Wideband Amplifier in 40-nm CMOS",
            "Optical links are common in datacenters. The amplifier has 45-GHz bandwidth.",
        )

        self.assertEqual(optical["link_medium"], "optical")
        self.assertEqual(optical["medium_source"], "abstract")
        self.assertEqual(background_only["link_medium"], "unspecified")

    def test_link_medium_preserves_explicit_mixed_and_generic_circuit_cases(self):
        mixed = [
            "A Demultiplexer for Wireline and Optical Receivers",
            "Serial Links Over Backplane and Multimode Fiber",
            "PAM-8 Electrical and PAM-4 VCSEL-Based Transmission",
        ]
        for title in mixed:
            with self.subTest(title=title):
                self.assertEqual(
                    classify_link_medium(title)["link_medium"], "unspecified",
                )
        self.assertEqual(
            classify_link_medium(
                "A 150-Gb/s PAM-4 Wideband Amplifier in 40-nm CMOS",
            )["link_medium"],
            "unspecified",
        )
        self.assertEqual(
            classify_link_medium(
                "A Reference-Less Single-Loop Half-Rate Binary CDR",
            )["link_medium"],
            "unspecified",
        )

    def test_link_medium_can_use_existing_measurement_class_without_overloading_it(self):
        optical = classify_link_medium(
            "A Receiver", measurement_link_classes=["optical"],
        )
        electrical = classify_link_medium(
            "An Interface", measurement_link_classes=["memory"],
        )

        self.assertEqual(optical["link_medium"], "optical")
        self.assertEqual(electrical["link_medium"], "electrical")

    def test_core_screening_is_a_low_confidence_electrical_default(self):
        core = classify_link_medium(
            "A Reference-Less Single-Loop Half-Rate Binary CDR",
            screening_class="core",
        )
        adjacent = classify_link_medium(
            "A Reference-Less Single-Loop Half-Rate Binary CDR",
            screening_class="adjacent",
        )
        optical_boundary = classify_link_medium(
            "A 25-Gb/s Burst-Mode CDR in 65-nm CMOS",
            screening_class="core",
        )

        self.assertEqual(core["link_medium"], "electrical")
        self.assertEqual(core["medium_source"], "screening")
        self.assertEqual(adjacent["link_medium"], "unspecified")
        self.assertEqual(optical_boundary["link_medium"], "unspecified")
        self.assertIn("core_optical_boundary", optical_boundary["medium_reason_codes"])

    def test_taxonomy_extracts_signaling_block_rate_and_energy(self):
        result = app_module._serdes_taxonomy(
            "A 1.5-pJ/b 112-Gb/s PAM-4 Transmitter With 8-Tap FFE"
        )

        self.assertIn("PAM-4", result["signals"])
        self.assertIn("TX", result["blocks"])
        self.assertIn("Equalizer", result["blocks"])
        self.assertEqual(result["rates"], ["112 Gb/s"])
        self.assertEqual(result["energy"], ["1.5 pJ/b"])

    def test_taxonomy_normalizes_terabit_rate(self):
        result = app_module._serdes_taxonomy("A 1.6-Tb/s Die-to-Die SerDes")

        self.assertEqual(result["rates"], ["1600 Gb/s"])
        self.assertIn("System", result["blocks"])

    def test_multilane_rate_preserves_lane_and_aggregate_scope(self):
        result = extract_performance("A 4×112-Gb/s PAM-4 Transceiver")

        self.assertEqual(result["lane_rate_gbps"], 112.0)
        self.assertEqual(result["lane_count"], 4)
        self.assertEqual(result["aggregate_rate_gbps"], 448.0)
        self.assertEqual(result["rate_scope"], "lane")

    def test_femtojoules_are_normalized_to_picojoules(self):
        result = extract_performance("A 77 fJ/bit receiver")

        self.assertAlmostEqual(result["energy_pj_bit"], 0.077)

    def test_power_parser_rejects_model_suffix_and_preserves_microwatts(self):
        model = extract_performance("A 2B4L4W equalizer architecture")
        micro = extract_performance(r"36 $\mu$W Off-State Power")
        watt = extract_performance("The full link consumes 1.8 W")
        milliwatt = extract_performance("A 14.7-mW receiver")

        self.assertIsNone(model["power_mw"])
        self.assertAlmostEqual(micro["power_mw"], 0.036)
        self.assertAlmostEqual(watt["power_mw"], 1800.0)
        self.assertAlmostEqual(milliwatt["power_mw"], 14.7)

    def test_power_per_rate_is_energy_not_absolute_power(self):
        regular = extract_performance("A 0.5 mW/Gb/s equalizer")
        shorthand = extract_performance("A 1.42 mW/Gb receiver")
        normalized = extract_performance(r"A 20.4 $\mu$W/Gb/s/dB receiver")
        lost_prefix = extract_performance("A reported FoM of 20.4 W/Gb/s/dB")

        self.assertEqual(regular["energy_pj_bit"], 0.5)
        self.assertIsNone(regular["power_mw"])
        self.assertEqual(shorthand["energy_pj_bit"], 1.42)
        self.assertIsNone(shorthand["power_mw"])
        self.assertAlmostEqual(
            normalized["energy_loss_normalized_pj_bit_db"], 0.0204
        )
        self.assertIsNone(normalized["power_mw"])
        self.assertIsNone(lost_prefix["energy_loss_normalized_pj_bit_db"])
        self.assertIn(
            "energy_loss_normalized_pj_bit_db", lost_prefix["ambiguous_fields"]
        )

    def test_multiple_abstract_energy_values_require_review(self):
        result = extract_performance(
            "The TX reaches 0.36 pJ/b while the clock path reaches 0.07 pJ/b."
        )

        self.assertIsNone(result["energy_pj_bit"])
        self.assertIn("energy_pj_bit", result["ambiguous_fields"])

    def test_optical_wavelength_is_not_a_process_node(self):
        result = extract_performance("An 850-nm VCSEL link fabricated in 40-nm CMOS")

        self.assertEqual(result["process_nm"], 40.0)

    def test_optical_bandwidth_and_cmos_compatibility_are_not_process_nodes(self):
        result = extract_performance(
            "A CMOS-compatible silicon-photonic interface with 50-nm optical bandwidth"
        )

        self.assertIsNone(result["process_nm"])

    def test_micron_process_node_is_normalized_to_nanometers(self):
        result = extract_performance("A 10-Gb/s receiver in 0.18-um CMOS")

        self.assertEqual(result["process_nm"], 180.0)

    def test_byte_rate_is_not_silently_treated_as_bit_rate(self):
        result = extract_performance("A 1.15 TB/s HBM3 interface")

        self.assertIsNone(result["reported_rate_gbps"])

    def test_density_metrics_do_not_become_rate_or_energy(self):
        rate = extract_performance("A 0.448-Tb/s/mm interface")
        energy = extract_performance("A 0.1-pJ/b/dB receiver")
        energy_bit = extract_performance("A 0.055-pJ/bit/dB transceiver")
        converted = extract_performance("A 0.2 mW/(Gb/s)/dB equalizer")

        self.assertIsNone(rate["reported_rate_gbps"])
        self.assertEqual(rate["throughput_density_gbps_per_mm"], 448.0)
        self.assertIsNone(energy["energy_pj_bit"])
        self.assertEqual(energy["energy_loss_normalized_pj_bit_db"], 0.1)
        self.assertIsNone(energy_bit["energy_pj_bit"])
        self.assertEqual(energy_bit["energy_loss_normalized_pj_bit_db"], 0.055)
        self.assertIsNone(converted["energy_pj_bit"])
        self.assertEqual(converted["energy_loss_normalized_pj_bit_db"], 0.2)

    def test_rate_range_is_not_silently_reduced_to_one_value(self):
        result = extract_performance("A 2.2-11.4 Gb/s serial link")

        self.assertIsNone(result["reported_rate_gbps"])
        self.assertEqual(result["reported_rate_min_gbps"], 2.2)
        self.assertEqual(result["reported_rate_max_gbps"], 11.4)

    def test_ber_and_fec_scope_are_preserved(self):
        result = extract_performance("The link reaches a post-FEC BER below 1E-12.")

        self.assertEqual(result["ber"], 1e-12)
        self.assertEqual(result["ber_scope"], "post_fec")

    def test_unknown_block_is_not_mislabeled_as_system(self):
        result = app_module._serdes_taxonomy("A 64-Gb/s low-jitter clock multiplier")

        self.assertEqual(result["blocks"], ["Unclassified"])

    def test_ieee_link_is_canonicalized_without_proxy(self):
        self.assertEqual(
            canonical_ieee_url(
                "9056913",
                "https://ieeexplore-ieee-org-ssl.access.yonsei.ac.kr/document/9056913/",
            ),
            "https://ieeexplore.ieee.org/document/9056913/",
        )

    def test_serdes_technology_family_distinguishes_cmos_and_bicmos(self):
        self.assertEqual(
            app_module._serdes_technology_family("130-nm SiGe BiCMOS"),
            "BiCMOS",
        )
        self.assertEqual(
            app_module._serdes_technology_family("3-nm FinFET CMOS"),
            "CMOS",
        )
        self.assertEqual(
            app_module._serdes_technology_family("250-nm InP DHBT"),
            "Other / unspecified",
        )

    def test_serdes_schema_migration_contains_five_additive_tables(self):
        path = Path(app_module.ROOT) / "scripts" / "migrations" / "001_serdes_measurements.sql"
        statements = split_sql_statements(path.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 5)
        self.assertTrue(all("CREATE TABLE IF NOT EXISTS" in item for item in statements))

    def test_screening_schema_adds_run_and_paper_audit_tables(self):
        path = Path(app_module.ROOT) / "scripts" / "migrations" / "002_serdes_screening.sql"
        statements = split_sql_statements(path.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertTrue(all("CREATE TABLE IF NOT EXISTS" in item for item in statements))

    def test_metadata_fetch_state_schema_is_additive(self):
        path = Path(app_module.ROOT) / "scripts" / "migrations" / "003_metadata_fetch_state.sql"
        statements = split_sql_statements(path.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 1)
        self.assertIn("CREATE TABLE IF NOT EXISTS paper_metadata_fetch_state", statements[0])

    def test_ptl_screening_keeps_direct_serdes_circuits(self):
        result = screen_serdes_relevance(
            "A 112-Gb/s PAM-4 SerDes Receiver in 7-nm CMOS",
            "The wireline receiver achieves 1.2 pJ/bit over a 28-dB channel.",
        )

        self.assertEqual(result["relevance_class"], "core")
        self.assertTrue(result["include_in_survey"])
        self.assertIn("direct_serdes", result["reason_codes"])

    def test_ptl_screening_separates_adjacent_optical_io(self):
        result = screen_serdes_relevance(
            "A 100-Gb/s VCSEL Transmitter With a Low-Power Driver",
            "The optical-I/O endpoint reports high-speed operation.",
        )

        self.assertEqual(result["relevance_class"], "adjacent")
        self.assertTrue(result["include_in_survey"])

    def test_ptl_screening_rejects_rate_only_transport(self):
        result = screen_serdes_relevance(
            "1-Tb/s Coherent WDM Transmission Over 80-km Fiber",
            "A long-haul optical transport experiment is demonstrated.",
        )

        self.assertEqual(result["relevance_class"], "out_of_scope")
        self.assertFalse(result["include_in_survey"])
        self.assertIn("transport_experiment", result["reason_codes"])

    def test_screening_rejects_editorial_front_matter(self):
        result = screen_serdes_relevance(
            "Editorial Special Section on High-Performance Wireline Transceiver Circuits",
            None,
        )

        self.assertEqual(result["relevance_class"], "out_of_scope")
        self.assertIn("non_research", result["reason_codes"])

    def test_screening_rejects_rf_carrier_receiver_without_link_rate(self):
        result = screen_serdes_relevance(
            "A 300-GHz Band Sliding-IF I/Q Receiver Front-End in 130-nm SiGe Technology",
            "The receiver targets a sub-THz wireless link.",
        )

        self.assertEqual(result["relevance_class"], "out_of_scope")
        self.assertIn("rf_carrier", result["reason_codes"])

    def test_screening_rejects_biosensor_front_end(self):
        result = screen_serdes_relevance(
            "A 4x4 Biosensor Array With a Low-Power Current-Sensitive Front-End",
            "The integrated CMOS biosensor measures molecular concentration.",
        )

        self.assertEqual(result["relevance_class"], "out_of_scope")
        self.assertIn("sensing_application", result["reason_codes"])

    def test_screening_routes_wireline_review_to_manual_queue(self):
        result = screen_serdes_relevance(
            "Design Techniques for CMOS Wireline NRZ Receivers Up To 56 Gb/s",
            "This tutorial reviews energy-efficient receiver design methods.",
        )

        self.assertEqual(result["relevance_class"], "needs_review")
        self.assertFalse(result["include_in_survey"])
        self.assertIn("review_article", result["reason_codes"])

    def test_screening_real_title_gold_set(self):
        cases = {
            "core": [
                "A 56 Gb/s/wire, 0.75 pJ/b Transceiver for Die-to-Die Interface Using Simultaneous Bi-Directional Signaling",
                "A 4nm 32Gb/s 8Tb/s/mm Die-to-Die Chiplet Using NRZ Single-Ended Transceiver With Equalization Schemes And Training Techniques",
                "10Gb/s serial I/O receiver based on variable reference ADC",
                "A 112Gb/s DAC-Based PAM-4 Transmitter with Fast Automatic Retiming Clock Phase Optimization and 6-Tap FFE in 28nm CMOS",
                "A Reference-Less Single-Loop Half-Rate Binary CDR",
                "A Dual-Loop Clock and Data Recovery Circuit With Compact Quarter-Rate CMOS Linear Phase Detector",
                "A Decision Feed-Forward Equalizer with Error Detection and Correction for 200-400Gbps Wireline Applications",
                "A 0.29pJ/b 5.27Tb/s/mm UCIe Advanced Package Link in 3nm FinFET with 2.5D CoWoS Packaging",
                "9.3 NVLink-C2C: A Coherent Off Package Chip-to-Chip Interconnect with 40Gbps/pin Single-ended Signaling",
                "A 40Gb/s decision feedback equalizer using back-gate feedback technique",
                "A 5–8 Gb/s low-power transmitter with 2-tap pre-emphasis based on toggling serialization",
            ],
            "adjacent": [
                "A 23-mW 30-Gb/s digitally programmable limiting amplifier for 100GbE optical receivers",
                "A 150-Gb/s PAM-4 Wideband Amplifier With Compact Footprint in 40-nm CMOS",
                r"A 3-nm FinFET CMOS PAM-4 Retimer for $\mathbf{8 0 0 - Gb} / \mathbf{s}$ and $\mathbf{1. 6 - T b} / \mathbf{s}$ Optical Modules",
                "A 100-Gb/s Burst-Mode Optical Receiver With 9-Tap Pipelined FFE and Amplitude/Phase Adaptation in 28-nm CMOS",
                "An 18.4Gb/s/pin Simultaneous Bidirectional Transceiver for Post HBM4 Using 4-Phase Hybrid Timing Alignment and Dual Equalization in 1b-nm DRAM Technology",
                "A 15 Gb/s Non-Return-to-Zero Transmitter With 1-Tap Pre-Emphasis Feed-Forward Equalizer for Low-Power Ground Terminated Memory Interfaces",
                "A 0.58-pJ/bit 56-Gb/s PAM-4 Optical Receiver Frontend with an Envelope Tracker for Co-Packaged Optics in 40-nm CMOS",
                "A 6.4Gb/s/pin HBM3 Digital PHY with Low-Power, Area-Efficient Techniques for Chiplet-Based AI processors in 12-nm CMOS",
                "A 120 GBd 2.8 Vpp Low-Power Differential Driver for InP Mach-Zehnder Modulator Using 55-nm SiGe HBTs",
                "Demonstration of Error-Free 32-Gb/s Operation From Monolithic CMOS Nanophotonic Transmitters",
                "A 10Gb/s photonic modulator and WDM MUX/DEMUX integrated with electronics in 0.13um SOI CMOS",
                "A 48GHz BW 225mW/ch Linear Driver IC in 130nm SiGe BiCMOS for Beyond-400Gb/s Coherent Optical Transmitters",
                "A Monolithically-Integrated Chip-to-Chip Optical Link in Bulk CMOS",
            ],
            "needs_review": [
                "Design Techniques for Single-Ended Wireline Crosstalk Cancellation Receiver Up To 112 Gb/s",
                "Equalizer Design and Performance Trade-Offs in ADC-Based Serial Links",
                "Recurrent Neural Network Equalization for Wireline Communication Systems",
                "Fixed-Point Implementation Analysis of MLSE Receiver DSP for High-Speed Wireline Transceivers",
                "A Continuous-Time Linear Equalizer With Ultrafine Gain Adjustment Achieving 0.3-dB DC-Gain Step and 0.9-dB Peaking-Gain Step",
                "The Design Techniques for High-Speed PAM4 Clock and Data Recovery",
                "Critical-Timing-Relaxed pre-Decision Feedforward Equalizer for High-Speed DSP-based Wireline Receiver using Hardware-in-the-Loop Verification on RF-SoC",
                "Analysis of UCIe 48/64-GT/s Electrical Links",
                "A Lightweight 3D D2D Interface for Active Interposer Chiplet Systems",
                "An Efficient Error Control Scheme for Chip-to-Chip Optical Interconnects",
                "Progress and trends in multi-Gbps optical receivers with CMOS integrated photodetectors",
                "Experimental Assessment of Interactions Between Nonlinear Impairments and Polarization-Mode Dispersion in 100-Gb/s Coherent Systems Versus Receiver Complexity",
                "Far-End Crosstalk Cancellation With MIMO OFDM for >200 Gb/s ADC-Based Serial Links",
                "Packaged Monolithic Silicon 112-Gb/s Coherent Receiver",
            ],
            "out_of_scope": [
                "A Fully Integrated 60 GHz 10 Gb/s QPSK Transceiver with Digital Transmitter and T/R Switch in 65nm CMOS",
                "A 37-43.5-GHz Fully Integrated 16-Element Phased-Array Transceiver With 64-QAM 7.2-Gb/s Data Rates Supporting Dual-Polarized MIMO",
                "A 125 um-Pitch-Matched Transceiver ASIC With Micro-Beamforming ADC and Multi-Level Signaling for 3-D Transfontanelle Ultrasonography",
                "A Design-Oriented Soft Error Rate Variation Model Accounting for Both Die-to-Die and Within-Die Variations in Submicrometer CMOS SRAM Cells",
                "A 65nm 8GHz E-PPWM/FSK IR-UWB Transceiver Achieving 2.4Gb/s Data Rate and 8.6pJ/b Energy Efficiency",
                r"A 640-Gb/s $4\times 4$-MIMO D-Band CMOS Transceiver Chipset",
                "Editorial Special section on High-Performance Wireline Transceiver Circuits",
                "A 129-Gb/s CMOS W-Band Polarization-Modulated Active Relay Transceiver Supporting Concurrent Dual-Polarized Dual-Stream Relay Operation",
                "A 32-Gb/s CMOS Receiver With Analog Carrier Recovery and Synchronous QPSK Demodulation",
                "Secure Scan Architecture Using Clock and Data Recovery Technique",
                "Notice of Violation of IEEE Publication Principles: A 54 dBOmega 10 Gb/s SiGe transimpedance-limiting amplifier",
                "Experimental Study of MLSE Receivers in the Presence of Narrowband Optical Filtering",
                "30-39GHz 2Gbit/s ring oscillator based OOK-modulator for chip-to-chip communications",
                "A 0.13um CMOS SoC for all-format blue and red laser DVD front-end digital signal processor",
                "Correction on ‘A 5-Gb/s/pin Transceiver With a Source-Synchronous Interface’",
                "Silver Stripe Optical Waveguide for Chip-to-Chip Optical Interconnections",
                "160-Gb/s optical clock recovery using a regeneratively mode-locked laser diode",
                "A 2 Gb/s 5.6 mW Digital LOS/NLOS Equalizer for the 60 GHz Band",
                "A 9 Gb/s 1.1 Vpp Precision Single-Ended Pin Electronics Driver in 40nm CMOS",
            ],
        }

        for expected, titles in cases.items():
            for title in titles:
                with self.subTest(expected=expected, title=title):
                    result = screen_serdes_relevance(title)
                    self.assertEqual(result["relevance_class"], expected)

    def test_screening_rejects_contactless_application_from_abstract(self):
        result = screen_serdes_relevance(
            "A 5.2 Gb/s 4.7 pJ/bit Capacitively-Coupled Transceiver With CDR",
            "A contactless transceiver communicates across a 3 mm air gap.",
        )

        self.assertEqual(result["relevance_class"], "out_of_scope")

    def test_screening_keeps_hyphenated_high_speed_tia_and_driver_modulator(self):
        titles = [
            "A 50-Gbaud Linear Burst-Mode Trans-Impedance Amplifier in 28 nm CMOS Technology",
            "A 36 GHz Bandwidth Driver Modulator in 28 nm CMOS",
        ]

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(
                    screen_serdes_relevance(title)["relevance_class"],
                    "adjacent",
                )

    def test_screening_rejects_passive_and_rf_photonic_boundary_cases(self):
        titles = [
            "160-Gb/s Polarization-Insensitive Demultiplexer Based on a Fiber-Optical Parametric Amplifier",
            "CMOS-Compatible and Temperature Insensitive C-Band Wavelength (De-)multiplexer",
            "A 3.9ns 8.9mW 4x4 silicon photonic switch hybrid integrated with CMOS driver",
            "A Monolithically Integrated ACP-OPLL Receiver for RF/Photonic Links",
            "Pulsenet - A Parallel Flash Sampler and Digital Processor IC for Optical SETI",
        ]

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(
                    screen_serdes_relevance(title)["relevance_class"],
                    "out_of_scope",
                )

    def test_screening_routes_transport_mux_demo_to_review(self):
        result = screen_serdes_relevance(
            "160-GBd (320-Gb/s) PAM4 Transmission Using 97-GHz Bandwidth Analog Multiplexer"
        )

        self.assertEqual(result["relevance_class"], "needs_review")

    def test_screening_keeps_memory_tia_and_parallel_bus_boundaries(self):
        cases = {
            "A 4-nm 1.15 TB/s HBM3 Interface With Resistor-Tuned Offset Calibration": "adjacent",
            "A 10-GHz Bandwidth Transimpedance Amplifier With Input DC Photocurrent Compensation Loop": "adjacent",
            "A 32Gb/s On-chip Bus with Driver Pre-emphasis Signaling": "adjacent",
        }

        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(
                    screen_serdes_relevance(title)["relevance_class"], expected
                )

    def test_screening_routes_test_and_invited_design_titles_to_review(self):
        titles = [
            "Testing SerDes beyond 4 Gbps - changing priorities",
            "Design of 224Gb/s DSP-Based Transceiver in CMOS Technology: Signal Integrity, Architecture, Circuits, and Packaging",
        ]

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(
                    screen_serdes_relevance(title)["relevance_class"],
                    "needs_review",
                )

    def test_screening_rejects_rf_memory_and_sensing_primary_papers(self):
        titles = [
            "A 0.2-to-22 GHz 24-Gbps Full-band Interference-Rejection 64QAM Receiver With Image and Blocker Rejection",
            "A 128Gb 3b/cell V-NAND flash memory with 1Gb/s I/O rate",
            "A 0.93 ps-ToF-Resolution NIRS IC for Psychiatric Disorders Diagnostics",
        ]

        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(
                    screen_serdes_relevance(title)["relevance_class"],
                    "out_of_scope",
                )

    def test_measured_cmos_optical_receiver_is_adjacent_without_rate(self):
        result = screen_serdes_relevance(
            "DC-Coupled Stacked-Differential Optical Receiver for IM-DD",
            "Fabricated in a standard 180-nm CMOS process, the receiver consumes 89.1 mW.",
        )

        self.assertEqual(result["relevance_class"], "adjacent")

    def test_optical_transport_and_photonic_devices_are_not_electronic_serdes(self):
        cases = [
            (
                "94-Gb/s 4-PAM Using an 850-nm VCSEL and Receiver Equalization",
                "Transmission over multimode fiber uses an offline digital equalizer.",
            ),
            (
                "Guardring-Free Planar AlInAs Avalanche Photodiodes for 2.5-Gb/s Receivers",
                "The external receiver also contains a SiGe TIA.",
            ),
            (
                "A 60GHz TIA in a Current-Reuse LO Generator With a Harmonic VCO",
                None,
            ),
        ]

        for title, abstract in cases:
            with self.subTest(title=title):
                self.assertEqual(
                    screen_serdes_relevance(title, abstract)["relevance_class"],
                    "out_of_scope",
                )

    def test_primary_sensing_is_out_but_link_auxiliary_sensor_is_kept(self):
        sensing = screen_serdes_relevance(
            "A CMOS Imager and Light-Pulse Receiver Array for Spatial Optical Communication"
        )
        link = screen_serdes_relevance(
            "A 10Gb/s 342fJ/bit Micro-Ring Modulator Transmitter With a Temperature Sensor in 65nm CMOS"
        )

        self.assertEqual(sensing["relevance_class"], "out_of_scope")
        self.assertEqual(link["relevance_class"], "adjacent")

    def test_screening_score_bands_do_not_overlap(self):
        examples = {
            "core": "A 112-Gb/s PAM-4 SerDes Receiver in 7-nm CMOS",
            "adjacent": "A 56-Gb/s PAM-4 Optical Receiver in 28-nm CMOS",
            "needs_review": "Equalizer Design Trade-Offs in ADC-Based Serial Links",
            "out_of_scope": "A 60-GHz QPSK Phased-Array Transceiver",
        }
        scores = {
            expected: screen_serdes_relevance(title)["relevance_score"]
            for expected, title in examples.items()
        }

        self.assertGreater(scores["core"], scores["adjacent"])
        self.assertGreater(scores["adjacent"], scores["needs_review"])
        self.assertGreater(scores["needs_review"], scores["out_of_scope"])

    def test_reference_measurement_outranks_abstract_and_title(self):
        base = {"review_status": "extracted", "updated_at": None}
        title = {**base, "source_kind": "title", "energy_pj_bit": 0.5}
        abstract = {**base, "source_kind": "abstract", "energy_pj_bit": 0.5}
        reference = {
            **base,
            "source_kind": "reference_xlsx",
            "review_status": "curated_reference",
            "energy_pj_bit": 0.5,
        }
        user_sheet = {
            **base,
            "source_kind": "user_sheet",
            "review_status": "verified",
            "energy_pj_bit": 0.5,
        }

        self.assertGreater(app_module._measurement_rank(user_sheet), app_module._measurement_rank(reference))
        self.assertGreater(app_module._measurement_rank(reference), app_module._measurement_rank(abstract))
        self.assertGreater(app_module._measurement_rank(abstract), app_module._measurement_rank(title))

    def test_chart_selection_filters_complete_operating_points_before_ranking(self):
        reference = {
            "id": 1, "implementation_id": 9, "source_kind": "reference_xlsx",
            "review_status": "curated_reference", "overall_confidence": 0.95,
            "evidence_count": 2, "updated_at": None, "reported_rate_gbps": 112.0,
            "energy_pj_bit": None, "process_nm": None,
        }
        abstract = {
            "id": 2, "implementation_id": 9, "source_kind": "abstract",
            "review_status": "extracted", "overall_confidence": 0.8,
            "evidence_count": 3, "updated_at": None, "reported_rate_gbps": 106.0,
            "energy_pj_bit": 1.2, "process_nm": 5.0,
        }

        selected = app_module._best_rows_for_implementations(
            [reference, abstract],
            lambda row: row.get("energy_pj_bit") is not None
            and row.get("process_nm") is not None,
        )

        self.assertEqual(selected[9]["id"], 2)

    def test_chart_selection_excludes_needs_review_candidates(self):
        candidate = {
            "id": 3, "implementation_id": 9, "source_kind": "user_sheet",
            "review_status": "needs_review", "overall_confidence": 0.7,
            "evidence_count": 3, "updated_at": None,
            "reported_rate_gbps": 40.0, "energy_pj_bit": 0.4,
            "process_nm": 28.0,
        }

        selected = app_module._best_rows_for_implementations(
            [candidate], lambda row: row.get("energy_pj_bit") is not None,
        )

        self.assertEqual(selected, {})

    def test_energy_chart_prefers_reported_value_within_same_review_tier(self):
        calculated_abstract = {
            "id": 4, "implementation_id": 9, "source_kind": "abstract",
            "review_status": "extracted", "overall_confidence": 0.8,
            "evidence_count": 3, "updated_at": None,
            "reported_rate_gbps": 56.0, "energy_pj_bit": 1.1,
            "energy_basis": "calculated_power_per_rate",
        }
        reported_title = {
            "id": 5, "implementation_id": 9, "source_kind": "title",
            "review_status": "extracted", "overall_confidence": 0.62,
            "evidence_count": 2, "updated_at": None,
            "reported_rate_gbps": 56.0, "energy_pj_bit": 1.2,
            "energy_basis": "reported_energy",
        }

        selected = app_module._best_rows_for_implementations(
            [calculated_abstract, reported_title],
            lambda row: row.get("energy_pj_bit") is not None,
            ranker=app_module._energy_measurement_rank,
        )

        self.assertEqual(selected[9]["id"], 5)

    def test_serializer_ic_is_core_serdes(self):
        decision = screen_serdes_relevance(
            "A single-40Gb/s dual-20Gb/s serializer IC with SFI-5.2 "
            "interface in 65nm CMOS"
        )

        self.assertEqual(decision["relevance_class"], "core")

    def test_filter_builder_keeps_values_parameterized(self):
        where, params = app_module._serdes_where(MultiDict([
            ("q", "PAM%' OR 1=1 --"),
            ("year_from", "2020"),
            ("year_to", "2025"),
            ("signal", "pam4"),
            ("block", "cdr"),
            ("medium", "optical"),
            ("subtype", "vcsel"),
            ("screening", "core"),
            ("pdf_only", "1"),
        ]))

        self.assertNotIn("OR 1=1", where)
        self.assertIn("OR 1=1", params["like"])
        self.assertEqual(params["year_from"], 2020)
        self.assertEqual(params["year_to"], 2025)
        self.assertIn("source_system IN ('ieee', 'optica')", where)
        self.assertIn("pdf_available = 1", where)
        self.assertIn("signal_pattern", params)
        self.assertIn("block_pattern", params)
        self.assertEqual(params["link_medium"], "optical")
        self.assertIn("serdes_paper_link_media", where)
        self.assertEqual(params["link_subtype"], "vcsel")
        self.assertIn("serdes_paper_subtype_overrides", where)
        self.assertIn("review_status IN ('verified','reviewed','approved','active')", where)
        self.assertEqual(params["screening"], "core")
        self.assertIn("serdes_paper_screenings", where)
        self.assertNotIn("serdes_pattern", params)

    def test_explorer_measurement_respects_selected_evidence_tier(self):
        connection = Mock()
        connection.execute.return_value.mappings.return_value.all.return_value = [
            {
                "paper_id": 1, "id": 10, "implementation_id": 100,
                "source_kind": "manual", "review_status": "verified",
                "overall_confidence": 1.0, "evidence_count": 2,
                "updated_at": None, "energy_pj_bit": 1.0,
            },
            {
                "paper_id": 1, "id": 11, "implementation_id": 100,
                "source_kind": "abstract", "review_status": "extracted",
                "overall_confidence": 0.8, "evidence_count": 1,
                "updated_at": None, "energy_pj_bit": 2.0,
            },
            {
                "paper_id": 2, "id": 12, "implementation_id": 200,
                "source_kind": "user_sheet", "review_status": "verified",
                "overall_confidence": 1.0, "evidence_count": 1,
                "updated_at": None, "energy_pj_bit": 0.8,
            },
        ]

        selected = app_module._best_measurements_for_papers(
            connection, [1], requested_tier="abstract",
        )

        self.assertEqual(selected[1]["measurement_id"], 11)
        user_sheet = app_module._best_measurements_for_papers(
            connection, [1, 2], requested_tier="user_sheet",
        )
        self.assertEqual(user_sheet[2]["measurement_id"], 12)
        query = str(connection.execute.call_args.args[0])
        self.assertIn("scope_override.review_status IN", query)
        self.assertIn("scope_override.reason", query)
        self.assertIn("scope_override.evidence_text", query)

    def test_fom_ready_requires_a_comparable_bit_rate(self):
        connection = Mock()
        connection.execute.return_value.mappings.return_value.all.return_value = [
            {
                "paper_id": 1, "source_kind": "abstract",
                "review_status": "extracted", "reported_rate_gbps": 56.0,
                "rate_scope": "unknown", "energy_pj_bit": 1.0,
                "process_nm": 28.0, "energy_component_scope": "trx",
            },
            {
                "paper_id": 2, "source_kind": "abstract",
                "review_status": "extracted", "lane_rate_gbps": 56.0,
                "rate_scope": "lane", "energy_pj_bit": 1.0,
                "process_nm": 28.0, "energy_component_scope": "trx",
            },
        ]
        papers = [
            {"id": 1, "paper_abstract": "a", "pdf_available": 0},
            {"id": 2, "paper_abstract": "b", "pdf_available": 0},
        ]

        completeness = app_module._paper_completeness_for_papers(
            connection, papers,
        )

        self.assertFalse(completeness[1]["same_point_fom_ready"])
        self.assertTrue(completeness[2]["same_point_fom_ready"])

    def test_reported_pareto_keeps_lane_and_aggregate_denominators_separate(self):
        base = {"link_medium": "optical", "energy_component_scope": "tx"}

        lane = app_module._pareto_group_key(
            {**base, "rate_scope": "lane"}, "reported",
        )
        aggregate = app_module._pareto_group_key(
            {**base, "rate_scope": "aggregate"}, "reported",
        )

        self.assertNotEqual(lane, aggregate)
        self.assertEqual(
            app_module._pareto_group_key(
                {**base, "rate_scope": "lane"}, "lane",
            ),
            app_module._pareto_group_key(
                {**base, "rate_scope": "aggregate"}, "lane",
            ),
        )

    def test_screened_filter_exposes_the_full_audited_corpus(self):
        where, params = app_module._serdes_where(MultiDict([
            ("screening", "screened"),
        ]))

        self.assertIn("serdes_paper_screenings", where)
        self.assertNotIn(app_module.SERDES_SCOPE_SQL, where)
        self.assertNotIn("serdes_pattern", params)
        self.assertIn("scope_name LIKE 'all%'", where)

    def test_audit_pipeline_uses_title_first_shortlist_fetch(self):
        project_root = Path(__file__).resolve().parents[1]
        source = (project_root / "scripts" / "audit_serdes_venues.py").read_text(
            encoding="utf-8"
        )

        self.assertLess(source.index('print("Pre-screen:", result)'), source.index('scope="screened"'))
        self.assertIn('print("Post-screen:", result)', source)
        self.assertNotIn('scope="venue"', source)

    def test_screening_rows_are_published_atomically(self):
        source = inspect.getsource(screen_venue)

        self.assertNotIn("index % 500", source)

    def test_primary_abstract_hits_checkpoint_before_exact_fallbacks(self):
        source = inspect.getsource(fetch_abstracts)

        primary_commit = source.index("# Persist every primary-query hit")
        fallback_loop = source.index("for paper in missing:")
        self.assertLess(primary_commit, fallback_loop)
        self.assertNotIn("records.update(client.fetch_batch", source)

    def test_default_filter_keeps_the_focused_serdes_scope(self):
        where, params = app_module._serdes_where(MultiDict())

        self.assertIn(app_module.SERDES_SCOPE_SQL, where)
        self.assertEqual(params["serdes_pattern"], app_module.SERDES_SQL_PATTERN)

    def test_authenticated_user_can_open_survey_page(self):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "viewer"
            sess["role"] = "viewer"
            sess["csrf_token"] = "test-token"

        response = client.get("/serdes")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SerDes Performance Survey", response.data)
        self.assertIn(b"Link Power Calculator", response.data)
        self.assertIn(b"/api/serdes/papers", response.data)
        self.assertIn(b"/api/serdes/performance", response.data)
        self.assertIn(b"Oregon State Wireline Link Survey", response.data)
        self.assertIn(b"Explore high-speed serial-link circuits", response.data)
        self.assertIn(b'id="point-popover"', response.data)
        self.assertIn(b"firstAuthorCitation", response.data)
        self.assertIn(b'data-zoom-chart="rate-chart"', response.data)
        self.assertIn(b'addEventListener("wheel"', response.data)
        self.assertIn(b"CHART_EDGE_GUTTER=20", response.data)
        self.assertIn(b"layout.dataPlotW||layout.plotW", response.data)
        self.assertIn(b'<option value="relevance">SerDes', response.data)
        self.assertIn(b'id="f-screening"', response.data)
        self.assertIn('<option value="screened">전수판정 전체'.encode(), response.data)
        self.assertIn(b'id="venue-audit-summary"', response.data)
        self.assertIn(b"meta.audit_years", response.data)
        self.assertIn(b"meta.audit_sources", response.data)
        self.assertIn(b"option.dataset.baseLabel", response.data)
        self.assertIn(b"data-generated", response.data)
        self.assertIn("차트의 n은 필요한 x·y".encode(), response.data)
        self.assertIn(b"Power[mW]", response.data)
        self.assertIn(b"P/R calculated", response.data)
        self.assertIn(b"calculated_only_energy_total", response.data)
        self.assertIn(b"calculated_power_per_rate", response.data)
        self.assertIn("WLink 원본 · 논문 미연결".encode(), response.data)
        self.assertIn(b"User Survey", response.data)
        self.assertIn(b'[${group.count}]', response.data)
        self.assertNotIn(b'id="survey-ai"', response.data)
        self.assertNotIn(b"serdes_ai.js", response.data)
        self.assertNotIn(b"serdes_ai.css", response.data)

    def test_survey_ai_ui_can_be_explicitly_reenabled(self):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
            sess["username"] = "viewer"
            sess["role"] = "viewer"
            sess["csrf_token"] = "test-token"

        with patch.object(app_module, "SURVEY_AI_UI_ENABLED", True):
            response = client.get("/serdes")

        self.assertIn(b'id="survey-ai"', response.data)
        self.assertIn(b"serdes_ai.js", response.data)
        self.assertIn(b"serdes_ai.css", response.data)
        self.assertNotIn(b".slice(-18)", response.data)
        self.assertIn(b'<option value="reported">Reported rate</option>', response.data)
        self.assertIn(b'id="performance-source-mode"', response.data)
        self.assertIn(b'id="performance-medium-mode"', response.data)
        self.assertIn(b'id="f-medium"', response.data)
        self.assertIn(b'function mediumBadge', response.data)
        self.assertIn(b'link_medium', response.data)
        self.assertIn(b'id="performance-table-note"', response.data)
        self.assertIn("대표 표본 · 최신 12 + 초기 8".encode(), response.data)
        self.assertIn("품질·인용 순위".encode(), response.data)
        self.assertIn(b"item.citation_count", response.data)
        self.assertIn(b"citation-count", response.data)
        self.assertIn(b'screening:"included"', response.data)
        self.assertIn(b'id="fom-rate-energy"', response.data)
        self.assertIn(b"const CAN_EDIT_FAVORITES=false", response.data)


if __name__ == "__main__":
    unittest.main()

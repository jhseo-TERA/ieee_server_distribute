"""Pure helpers for extracting and presenting SerDes performance clues.

The extractors in this module are intentionally conservative.  They preserve
ambiguous candidates instead of silently turning a range, a density, or two
different operating points into one benchmark value.  Database import and the
Flask UI share these helpers so that title and abstract handling stay aligned.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from html import unescape
import re
import unicodedata


SERDES_SQL_PATTERN = (
    r"serdes|serializer|deserializer|clock.{0,8}data recovery|clock recovery|"
    r"(^|[^a-z0-9])cdr([^a-z0-9]|$)|(^|[^a-z0-9])dfe([^a-z0-9]|$)|"
    r"(^|[^a-z0-9])ffe([^a-z0-9]|$)|(^|[^a-z0-9])ctle([^a-z0-9]|$)|"
    r"wire[- ]?line|serial[- ]?link|serial i/?o|serial interface|high[- ]speed i/?o|"
    r"retimer|chip[- ]to[- ]chip|die[- ]to[- ]die|chiplet|ucie|"
    r"pam[- ]?[2348]|duobinary|decision.feedback.equaliz|feed.forward.equaliz|"
    r"continuous.time.linear.equaliz|baud|gb/s|gbps|gbit/s"
)

SERDES_SCREENING_VERSION = "serdes-screen-2.3"
SCREENING_PATTERNS = {
    "direct_serdes": re.compile(
        r"\b(?:serdes|serializer|deserializer|wire[- ]?line|serial[- ]links?|"
        r"serial[- ]?(?:i/?o|interface|data)|chip[- ]to[- ]chip|die[- ]to[- ]die|"
        r"d2d (?:links?|interfaces?|interconnects?)|chiplet|ucie|"
        r"electrical i/?o|high[- ]speed i/?o)\b",
        re.IGNORECASE,
    ),
    "circuit_block": re.compile(
        r"\b(?:transceiver|transmitter|receiver|serializer|deserializer|retimer|"
        r"driver|front[- ]end|analog front[- ]end|afe|trans[- ]?impedance amplifier|tia|"
        r"clock(?:\s+and)?\s+data recovery|clock recovery|cdr|equaliz\w*|"
        r"dfe|ffe|ctle|slicer|phase interpolator|eye monitor)\b",
        re.IGNORECASE,
    ),
    "link_endpoint": re.compile(
        r"\b(?:transmitters?|receivers?|transceivers?|retimers?|drivers?|"
        r"serializers?|deserializers?|front[- ]?ends?|afe|tia|tx|rx)\b",
        re.IGNORECASE,
    ),
    "electrical_signaling": re.compile(
        r"\b(?:pam[- ]?[2348]|[2348][- ]?pam|nrz|duo[- ]?binary|multi[- ]level signaling)\b",
        re.IGNORECASE,
    ),
    "ic_evidence": re.compile(
        r"\b(?:cmos|bi[- ]?cmos|sige|finfet|fd[- ]?soi|integrated circuit|"
        r"asic|\d+(?:\.\d+)?\s*nm)\b",
        re.IGNORECASE,
    ),
    "efficiency_evidence": re.compile(
        r"(?:\b(?:energy efficiency|low[- ]power|power consumption)\b|"
        r"(?<![A-Za-z])(?:[pf]j\s*/\s*(?:b|bit)|mW)\b)",
        re.IGNORECASE,
    ),
    "optical_io": re.compile(
        r"\b(?:optical i/?o|optical interconnect|vcsel|silicon photonic|"
        r"silicon optical|photonic integrated|microring|micro[- ]ring|"
        r"optical transmitter|optical receiver|laser driver|modulator driver|driver modulator|"
        r"electro[- ]optic|ge[- ]on[- ]si\s+pd|transimpedance amplifier|tia)\b",
        re.IGNORECASE,
    ),
    "transport_experiment": re.compile(
        r"\b(?:transmission|fiber transmission|coherent transmission|"
        r"wavelength[- ]division multiplex|wdm|ofdm|passive optical network|pon|"
        r"\d+(?:\.\d+)?\s*km|long[- ]haul)\b",
        re.IGNORECASE,
    ),
    "device_physics": re.compile(
        r"\b(?:quantum[- ]dot|photonic crystal|grating coupler|waveguide|"
        r"semiconductor optical amplifier|laser|photodetector|photodiode|"
        r"modulator|optical switch|filter)\b",
        re.IGNORECASE,
    ),
    "algorithm_system": re.compile(
        r"\b(?:machine learning|neural network|digital signal processing|dsp|"
        r"equalization algorithm|forward error correction|fec|coding scheme|"
        r"maximum likelihood sequence|mlse|nonlinear compensation|"
        r"mimo reconstruction|mimo equaliz\w*|polarization crosstalk cancellation|"
        r"error[- ]control scheme|fourier transformation|"
        r"frequency[- ]domain equaliz\w*|training sequences?|ofdm|"
        r"(?:adaptive|blind|turbo|nonlinear|digital) equaliz\w*)\b",
        re.IGNORECASE,
    ),
    "sensing_application": re.compile(
        r"\b(?:sensors?|sensing (?:applications?|systems?|platforms?)|"
        r"lidar|range[- ]finding|distance ranging|"
        r"time[- ]of[- ]flight|tof|nirs|diagnostics?|imaging|"
        r"spectroscop\w*|biosens\w*|"
        r"refractive index|single[- ]photon|spad|gyroscope|wearable|biomedical|"
        r"neural recording|neural interface|bioz|ppg|exg|ultraso\w*|implantable|"
        r"medical implant|bodyid|imagers?|image sensors?|nmr|field probes?)\b",
        re.IGNORECASE,
    ),
    "nonwireline_link": re.compile(
        r"\b(?:radio[- ]over|free[- ]space|wireless|antenna|"
        r"visible[- ]light communications?|spatial optical communications?|"
        r"vlc|all[- ]optical|phased[- ]array|"
        r"radar|satellite|satcom|bluetooth|wi[- ]?fi|lte|nb[- ]?iot|"
        r"internet[- ]of[- ]things|body[- ]channel|human[- ]body communication|hbc|"
        r"uwb|impulse[- ]radio)\b",
        re.IGNORECASE,
    ),
    "rf_carrier": re.compile(
        r"\b(?:rf front[- ]end|millimeter[- ]wave|mm[- ]?wave|sub[- ]?thz|"
        r"d[- ]?band|g[- ]?band|w[- ]?band|direct[- ]conversion|beamform\w*|"
        r"[1-9]\d{1,3}(?:\.\d+)?\s*[-–]?\s*ghz(?:[- ]band)?)\b",
        re.IGNORECASE,
    ),
    "non_research": re.compile(
        r"^\s*(?:(?:guest\s+)?editorial\b|corrections?(?:\s+(?:to|on)\b|:)|erratum\b|"
        r"retraction\b|notice of violation\b|special (?:section|issue)\b|"
        r"table of contents\b|index\b|"
        r"front cover\b|back cover\b|call for papers\b|in memoriam\b|"
        r"session\s+\d+(?:\s+overview\b|\s*[-—:])|"
        r"forum\b|f\d+\s*:\s*|comments? on\b)",
        re.IGNORECASE,
    ),
    "review_article": re.compile(
        r"\b(?:review|survey|tutorial|overview|perspective|roadmap|recent advances|"
        r"challenges and solutions|future prospects|feasibility stud\w*|"
        r"performance assessment|experimental assessment|performance investigation|"
        r"progress and trends|impact of|effect of|part\s+(?:i|ii|iii|iv|v))\b|"
        r"^\s*(?:the\s+)?design (?:techniques|considerations|methodolog\w*)\b",
        re.IGNORECASE,
    ),
}

UNAMBIGUOUS_WIRELINE_RE = re.compile(
    r"\b(?:serdes|serializers?|deserializers?|lvds|wire[- ]?line|serial[- ]links?|"
    r"serial[- ]?(?:i/?o|interface|data)|displayport|hypertransport|"
    r"c[- ]?phy|d[- ]?phy|m[- ]?phy|pcie|pci express|sfi[- ]?\d*|xaui)\b",
    re.IGNORECASE,
)
DIRECT_LINK_CONTEXT_RE = re.compile(
    r"\b(?:serdes|serializers?|deserializers?|lvds|wire[- ]?line|serial[- ]links?|"
    r"serial[- ]?(?:i/?o|interface|data)|high[- ]speed links?|data links?|"
    r"on[- ]chip links?|usr links?|source[- ]synchronous|parallel interfaces?|"
    r"i/?o links?|on[- ]chip bus(?:es)?|parallel bus(?:es)?|short[- ]reach interconnects?|"
    r"loss channels?|"
    r"intra[- ]package|inter[- ]package|off[- ]package|"
    r"backplane(?: links?| transceivers?)?|forwarded[- ]clock i/?o|"
    r"displayport|hypertransport|c[- ]?phy|d[- ]?phy|m[- ]?phy|"
    r"pcie|pci express|sfi[- ]?\d*|xaui|"
    r"chip[- ]to[- ]chip|die[- ]to[- ]die|d2d (?:links?|interfaces?|interconnects?)|"
    r"ucie|aib[- ]compatible|"
    r"chiplet (?:links?|interconnects?|i/?o)|electrical i/?o|high[- ]speed i/?o)\b",
    re.IGNORECASE,
)
DIRECT_IMPLEMENTATION_CONTEXT_RE = re.compile(
    r"\b(?:serdes|serializers?|deserializers?|lvds|serial[- ]links?|"
    r"serial[- ]?(?:i/?o|interface|data)|high[- ]speed links?|data links?|"
    r"on[- ]chip links?|usr links?|source[- ]synchronous|parallel interfaces?|"
    r"i/?o links?|on[- ]chip bus(?:es)?|parallel bus(?:es)?|short[- ]reach interconnects?|"
    r"loss channels?|"
    r"intra[- ]package|inter[- ]package|off[- ]package|"
    r"backplane(?: links?| transceivers?)?|forwarded[- ]clock i/?o|"
    r"displayport|hypertransport|c[- ]?phy|d[- ]?phy|m[- ]?phy|"
    r"pcie|pci express|sfi[- ]?\d*|xaui|"
    r"chip[- ]to[- ]chip|die[- ]to[- ]die|d2d (?:links?|interfaces?|interconnects?)|"
    r"ucie|aib[- ]compatible|nvlink[- ]c2c|"
    r"chiplet (?:links?|interconnects?|i/?o)|electrical i/?o|high[- ]speed i/?o)\b",
    re.IGNORECASE,
)
PRIMARY_CDR_RE = re.compile(
    r"\b(?:clock\s*(?:(?:and|&)\s+)?data recovery|clock[- ]and[- ]data recovery|"
    r"clock\s*[/&-]\s*data recovery|clock recovery|cdr)\b",
    re.IGNORECASE,
)
EXPLICIT_CDR_RE = re.compile(
    r"\b(?:clock\s*(?:and|&)\s+data recovery|clock[- ]and[- ]data recovery|cdr)\b",
    re.IGNORECASE,
)
PRIMARY_ENDPOINT_RE = re.compile(
    r"\b(?:transmitters?|receivers?|transceivers?|serializers?|deserializers?|tx|rx)\b",
    re.IGNORECASE,
)
EQUALIZER_RE = re.compile(
    r"\b(?:equaliz\w*|dfe|ffe|ctle)\b",
    re.IGNORECASE,
)
CIRCUIT_EQUALIZER_RE = re.compile(
    r"\b(?:dfe|ffe|ctle|decision[- ]feedback equaliz\w*|"
    r"feed[- ]forward equaliz\w*|continuous[- ]time linear equaliz\w*)\b",
    re.IGNORECASE,
)
ADJACENT_BLOCK_RE = re.compile(
    r"\b(?:retimers?|limiting amplifiers?|wideband amplifiers?|"
    r"trans[- ]?impedance amplifiers?|tia|drivers?|front[- ]?ends?|afe|"
    r"samplers?|multiplexers?|mux|demultiplexers?|demux|"
    r"phase interpolators?)\b",
    re.IGNORECASE,
)
GENERIC_ENDPOINT_BLOCK_RE = re.compile(
    r"\b(?:drivers?|front[- ]?ends?|afe)\b",
    re.IGNORECASE,
)
OPTICAL_ANCHOR_RE = re.compile(
    r"\b(?:optical|photon\w*|vcsel|laser(?: diode)?|photodetectors?|photodiodes?|"
    r"optoelectronic|apd|mzms?|eml|microring|micro[- ]ring|"
    r"silicon photonic|si[- ]photonic|ge[- ]on[- ]si\s+pd|"
    r"nanophoton\w*|optics|dfb|tfln|"
    r"(?:e|g|xg|xgs|10g|25g|50g)?-?pon|"
    r"dwdm|wdm|otdm|fiber[- ]?optic|fibers?|pof|ssmf|smf|nzdsf|mmf|c[- ]band|"
    r"modulator drivers?|driver modulators?|m[- ]?z(?:m)?|mach[- ]zehnder|"
    r"co[- ]packaged optics?|cpo)\b",
    re.IGNORECASE,
)
COHERENT_RE = re.compile(r"\bcoherent\b", re.IGNORECASE)
HIGH_SPEED_TIA_RE = re.compile(
    r"\b(?:trans[- ]?impedance(?:[- ]?agc)? amplifiers?|tia)\b",
    re.IGNORECASE,
)
MONOLITHIC_OPTICAL_RE = re.compile(
    r"\b(?:monolithic(?:ally)?|heterogeneously integrated|"
    r"electronic[- ]photonic co[- ]design\w*)\b",
    re.IGNORECASE,
)
OPTICAL_LINK_HARDWARE_RE = re.compile(
    r"\b(?:optical (?:links?|interconnects?|i/?o)|chiplet optical i/?o|"
    r"co[- ]packaged optics?|cpo|mrm[- ]based)\b",
    re.IGNORECASE,
)
INTEGRATED_OPTICAL_ENDPOINT_RE = re.compile(
    r"\b(?:silicon photonics? transceivers?|optical transceiver engines?|"
    r"(?:monolithic(?:ally)? |fully )?integrated optical receivers?|"
    r"optical receivers? with integrated|latch[- ]type optical receivers?)\b",
    re.IGNORECASE,
)
OPTICAL_DRIVER_CIRCUIT_RE = re.compile(
    r"\b(?:vcsel over[- ]?driving ics?|vcsel drivers?|"
    r"modulator transmitters?|modulator drivers?|driver modulators?)\b",
    re.IGNORECASE,
)
TIA_BANDWIDTH_RE = re.compile(
    r"(?:\b(?:trans[- ]?impedance(?:[- ]?agc)? amplifiers?|tia)\b.{0,45}"
    r"\d+(?:\.\d+)?\s*[-–]?\s*GHz\b|"
    r"\b\d+(?:\.\d+)?\s*[-–]?\s*GHz\b.{0,45}"
    r"\b(?:trans[- ]?impedance(?:[- ]?agc)? amplifiers?|tia)\b)",
    re.IGNORECASE,
)
OPTICAL_LINK_TRANSLATOR_RE = re.compile(
    r"\b(?:electronic bus to .{0,30}optical link|"
    r"optical link .{0,25}(?:translator|bridge))\b",
    re.IGNORECASE,
)

# Link medium is deliberately separate from ``serdes_measurements.link_class``.
# The latter contains a mixture of topology (backplane, die-to-die, memory) and
# medium (optical), while one optical die-to-die link legitimately has both.
LINK_MEDIUM_VERSION = "serdes-medium-1.0"
LINK_MEDIUM_OPTICAL_RE = re.compile(
    r"\b(?:optical(?:\s+(?:i/?o|links?|interconnects?|channels?|interfaces?|"
    r"receivers?|transmitters?|transceivers?|modules?|engines?|transport))?|"
    r"photon\w*|silicon[- ]photon\w*|si[- ]photon\w*|opto[- ]?electronic|"
    r"vcsel|photodetectors?|photodiodes?|avalanche photodiodes?|apd|"
    r"laser(?: diode)?|eml|dfb|fiber[- ]?optic|multimode fibers?|single[- ]mode fibers?|"
    r"microrings?|micro[- ]rings?|mach[- ]zehnder|mzms?|modulator drivers?|"
    r"driver modulators?|co[- ]packaged optics?|cpo|"
    r"(?:e|g|xg|xgs|10g|25g|50g)?-?pon|dwdm|cwdm|wdm|otdm|"
    r"100gbase[- ]lr4|100gbase[- ]sr4|400gbase[- ]fr4|800gbase[- ]fr4)\b",
    re.IGNORECASE,
)
LINK_MEDIUM_ELECTRICAL_RE = re.compile(
    r"\b(?:electrical\s+(?:i/?o|links?|interconnects?|channels?|interfaces?|"
    r"signaling|transmission|backplanes?|cables?)|wire[- ]?line|copper(?:\s+cables?)?|"
    r"twinax|backplane|pcb\s+(?:channels?|traces?|links?)|"
    r"die[- ]to[- ]die|chip[- ]to[- ]chip|d2d\s+(?:links?|interfaces?|interconnects?)|"
    r"chiplets?|ucie|aib(?:[- ]compatible)?|nvlink[- ]c2c|"
    r"ddr\d*|lpddr\d*|gddr\d*|hbm\d*|memory\s+(?:i/?o|interfaces?)|"
    r"pcie|pci express|displayport|usb(?:\s*\d(?:\.\d)?)?|sata|xaui|sfi[- ]?\d*)\b",
    re.IGNORECASE,
)
LINK_MEDIUM_GENERIC_ELECTRICAL_RE = re.compile(
    r"\b(?:serdes|serializers?|deserializers?|serial[- ]links?|"
    r"serial[- ]?(?:i/?o|interfaces?|data)|retimers?|high[- ]speed i/?o)\b",
    re.IGNORECASE,
)
LINK_MEDIUM_CORE_AMBIGUOUS_RE = re.compile(
    r"\b(?:burst[- ]mode|coherent|trans[- ]?impedance|tia|"
    r"dml|directly[- ]modulated|distributed[- ]feedback laser|dfb|"
    r"sonet|tosa|rosa|dispersion compensat\w*|chirp[- ]managed)\b",
    re.IGNORECASE,
)
LINK_MEDIUM_ABSTRACT_IMPLEMENTATION_RE = re.compile(
    r"\b(?:we\s+(?:present|demonstrate|propose|report)|this\s+(?:work|paper|design|"
    r"receiver|transmitter|transceiver|serdes|circuit)|proposed|presented|"
    r"designed|implemented|fabricated|demonstrated|targets?|intended|"
    r"used\s+(?:for|in)|for\s+use\s+in|operat(?:es|ing)|tested\s+(?:over|with))\b",
    re.IGNORECASE,
)
LINK_MEDIUM_EXPLICIT_MIXED_RE = re.compile(
    r"(?:\b(?:electrical|wire[- ]?line|backplane|copper|pcb)\b.{0,75}"
    r"\b(?:and|or|versus|vs\.?)\b.{0,75}"
    r"\b(?:optical|photon\w*|vcsel|fibers?)\b|"
    r"\b(?:optical|photon\w*|vcsel|fibers?)\b.{0,75}"
    r"\b(?:and|or|versus|vs\.?)\b.{0,75}"
    r"\b(?:electrical|wire[- ]?line|backplane|copper|pcb)\b)",
    re.IGNORECASE,
)
LINK_MEDIUM_MEASUREMENT_ELECTRICAL = frozenset(
    {"backplane", "cable", "memory", "die_to_die", "chip_to_chip", "electrical"}
)
CDR_CIRCUIT_ANCHOR_RE = re.compile(
    r"\b(?:reference[- ]less|half[- ]rate|quarter[- ]rate|full[- ]rate|"
    r"single[- ]loop|dual[- ]loop|phase detectors?|jitter tolerance|"
    r"frequency acquisition|dco[- ]based)\b",
    re.IGNORECASE,
)
CDR_CIRCUIT_TITLE_RE = re.compile(
    r"(?:clock\s*(?:(?:and|&|/)\s*)?data recovery|"
    r"clock[- ](?:and[- ])?data recovery|\bcdr\b).{0,16}\bcircuits?\b",
    re.IGNORECASE,
)
PASSIVE_OPTICAL_COMPONENT_RE = re.compile(
    r"\b(?:parametric (?:devices?|gates?|amplifiers?)|photonic switch(?:es)?|"
    r"optical waveguides?|grating couplers?|"
    r"wavelength\s*\(?de[- ]?\)?multiplexers?)\b",
    re.IGNORECASE,
)
STRONG_PASSIVE_OPTICAL_RE = re.compile(
    r"\b(?:holograms?|optical backplanes?|grating couplers?|"
    r"multicore(?: multimode)? fibers?|plastic optical fibers?|optical subassemblies?|"
    r"tosa|arrayed waveguide gratings?|awg|laser arrays?|"
    r"photonic devices?|silicon photonic interfaces?|"
    r"cryogenic.{0,40}vcsel)\b",
    re.IGNORECASE,
)
PHOTONIC_DEVICE_PLATFORM_RE = re.compile(
    r"\b(?:dual[- ]quantum[- ]well|balanced detection|vcsel arrays?|dfbs?|"
    r"comb[- ]based|photonic coherent|mach[- ]zehnder transmitters?|"
    r"cwdm receivers?|dqpsk receivers?|inP(?:\s|$)|"
    r"alumina substrates?|thick film on ceramics|lattice[- ]filter|lan[- ]wdm|"
    r"photonic reservoir|optical equalizers? based on photonic integration|"
    r"hybrid(?:ly)? integrated"
    r".{0,35}optical receivers?)\b",
    re.IGNORECASE,
)
PRIMARY_PHOTONIC_DEVICE_RE = re.compile(
    r"\b(?:avalanche photodiodes?|guardring[- ]free .{0,35}photodiodes?|"
    r"lattice[- ]filter[- ]based|optical equalizers? based on photonic integration|"
    r"\d+(?:\.\d+)?[- ](?:gb/s|gbps).{0,45}\bvcsel\b)\b",
    re.IGNORECASE,
)
RF_MODE_RE = re.compile(
    r"\b(?:rf|qpsk|qam|mimo|direct[- ]conversion|phased[- ]array|beamform\w*|"
    r"antennas?|radar|ook|fsk|gfsk|gnss|bluetooth|ble)\b",
    re.IGNORECASE,
)
EXPLICIT_RF_APPLICATION_RE = re.compile(
    r"\b(?:rf|microwave[- ]photonic|millimeter[- ]wave|mm[- ]?wave|"
    r"sub[- ]?thz|d[- ]band|g[- ]band|w[- ]band|j[- ]band|"
    r"radar|antennas?|wireless|cellular|bluetooth|gnss|rfid|"
    r"local oscillators?|lo generators?|vco)\b",
    re.IGNORECASE,
)
STRONG_SENSING_APPLICATION_RE = re.compile(
    r"\b(?:nirs|psychiatric|diagnostics?|biosensors?|nmr|field probes?|"
    r"ultraso\w*|endoscopic|time[- ]of[- ]flight|tof|imaging|image sensors?|"
    r"sensor data receivers?|imagers?|medical implants?|biomedical|lidar|"
    r"spectroscop\w*|neural recording|neural interface)\b",
    re.IGNORECASE,
)
RF_APPLICATION_RE = re.compile(
    r"\b(?:d[- ]band|g[- ]band|w[- ]band|millimeter[- ]wave|mm[- ]?wave|"
    r"sub[- ]?thz|j[- ]band|(?:\d+[- ]?)?fsk|gfsk|(?:\d+[- ]?)?ask|"
    r"frequency[- ]shift[- ]keying|amplitude[- ]shift[- ]keying|"
    r"gnss|bluetooth|ble|cellular|5g\s*nr|6g|802\.11\w*|rfid|bodyid|"
    r"contactless|beam[- ]?steer\w*|eirp|power amplifier|radar|antennas?|"
    r"local oscillators?|lo generators?|harmonic[- ]current filters?|"
    r"image[- ]rejection|blocker rejection|full[- ]band interference[- ]rejection|"
    r"software[- ]defined radio|microwave[- ]photonic|"
    r"rf\s*[/ -]\s*lfm|rf signals?|"
    r"photonic rf links?|rf\s*/?\s*photonic links?)\b|"
    r"\b(?:terahertz|thz).{0,35}(?:transmitters?|receivers?|transceivers?|"
    r"communications?)\b",
    re.IGNORECASE,
)
RF_CARRIER_TITLE_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?(?:\s*[-–/]\s*\d+(?:\.\d+)?)?\s*[-–]?\s*GHz\b.{0,35}"
    r"(?:transmitters?|receivers?|transceivers?|radios?|communications?)|"
    r"\b(?:transmitters?|receivers?|transceivers?|radios?|communications?).{0,35}"
    r"\d+(?:\.\d+)?(?:\s*[-–/]\s*\d+(?:\.\d+)?)?\s*[-–]?\s*GHz\b)",
    re.IGNORECASE,
)
RF_BANDWIDTH_OR_CLOCK_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*[-–]?\s*GHz\s*(?:BW|bandwidth|clock)\b|"
    r"\b(?:BW|bandwidth|clock|PLL|VCO)\b.{0,15}"
    r"\d+(?:\.\d+)?\s*[-–]?\s*GHz\b)",
    re.IGNORECASE,
)
CLOCKING_FREQUENCY_CONTEXT_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*[-–]?\s*GHz\b.{0,45}"
    r"(?:jitter|tracking|bandwidth|clock|oscillator)|"
    r"(?:jitter|tracking|bandwidth|clock|oscillator).{0,45}"
    r"\b\d+(?:\.\d+)?\s*[-–]?\s*GHz\b)",
    re.IGNORECASE,
)
RF_NAMED_BAND_RE = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*"
    r"[-–]?\s*GHz[- ]band\b",
    re.IGNORECASE,
)
RF_OOK_CARRIER_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*[-–]?\s*GHz\b.{0,80}\bOOK\b|"
    r"\bOOK\b.{0,80}\b\d+(?:\.\d+)?\s*[-–]?\s*GHz\b)",
    re.IGNORECASE,
)
RF_POWER_CARRIER_RE = re.compile(
    r"\b\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*"
    r"[-–]?\s*GHz\b.{0,70}\b(?:P(?:sat|out)|EIRP)\b|"
    r"\b(?:P(?:sat|out)|EIRP)\b.{0,70}\b\d+(?:\.\d+)?\s*"
    r"[-–]?\s*GHz\b",
    re.IGNORECASE,
)
IC_TECHNOLOGY_RE = re.compile(
    r"\b(?:m?cmos(?![- ]compatible)|bi[- ]?cmos|finfet|fd[- ]?soi|asic|"
    r"eics?|electronic integrated circuits?)\b",
    re.IGNORECASE,
)
IC_ABBREVIATION_RE = re.compile(r"\bICs?\b", re.IGNORECASE)
PHOTONIC_IC_RE = re.compile(
    r"\b(?:photonic|optical)\s+(?:integrated circuits?|ICs?)\b",
    re.IGNORECASE,
)
PROCESS_NODE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*nm\b|\b(0?\.\d+)\s*[µμu]m\b",
    re.IGNORECASE,
)
PROCESS_CONTEXT_RE = re.compile(
    r"\b(?:process(?:es)?|technology|node|fabricated|implemented|"
    r"cmos(?![- ]compatible)|bi[- ]?cmos|sige|finfet|fd[- ]?soi|soi|hbt|inp|gaas)\b",
    re.IGNORECASE,
)
MEASURED_IMPLEMENTATION_RE = re.compile(
    r"\b(?:fabricated|prototype|measurement results?|measured|implemented in|"
    r"electrical characterization|silicon[- ]proven|test chip)\b",
    re.IGNORECASE,
)
MEMORY_IO_RE = re.compile(
    r"\b(?:ddr\d*|gddr\d*|lpddr\d*|hbm\d*|e?dram|nand|memory interfaces?|"
    r"memory links?)\b",
    re.IGNORECASE,
)
MEMORY_ENDPOINT_BLOCK_RE = re.compile(
    r"\b(?:phy|physical layer|transceivers?|trx|rx|tx|drivers?|equaliz\w*|"
    r"(?:tsv|dq)?\s*i/?o(?: interfaces?| circuitry)?|"
    r"memory interfaces?|(?:hbm\d*|e?dram|ddr\d*|gddr\d*|lpddr\d*) interfaces?)\b",
    re.IGNORECASE,
)
MEMORY_TEST_RE = re.compile(
    r"\b(?:tests?|testing|testability|test methods?|testers?|ate|"
    r"automatic test equipment|bridges?|"
    r"built[- ]in self[- ]test|bist)\b",
    re.IGNORECASE,
)
METHOD_REVIEW_RE = re.compile(
    r"\b(?:review|survey|tutorial|overview|recent advances|modeling|analysis|"
    r"performance of|sensitivity of|"
    r"trade[- ]?offs?|hardware[- ]in[- ]the[- ]loop|co[- ]simulation|framework|"
    r"feasibility stud\w*|performance assessment|experimental assessment|"
    r"performance investigation|progress and trends|impact of|effect of|"
    r"measurement techniques?|test methods?|design methodolog\w*|"
    r"design and simulation|post[- ]layout simulation|testing serdes|"
    r"signal integrity, architecture, circuits, and packaging|"
    r"mimo equaliz\w*|polarization crosstalk cancellation|control schemes?|"
    r"experimental demonstration|receiver modules?|ber measurements?|"
    r"dispersion (?:pre)?compensation|using .{0,45} architectures?)\b|"
    r"^\s*reconfiguration in\b|"
    r"^\s*(?:the\s+)?design (?:techniques|considerations|methodolog\w*)\b",
    re.IGNORECASE,
)
ALGORITHM_STUDY_RE = re.compile(
    r"\b(?:experimental|simulation) stud(?:y|ies)\b",
    re.IGNORECASE,
)
PARALLEL_LINK_RE = re.compile(
    r"\b(?:parallel|source[- ]synchronous|aib[- ]compatible|aer links?|"
    r"on[- ]chip bus(?:es)?)\b",
    re.IGNORECASE,
)
SYSTEM_PLATFORM_RE = re.compile(
    r"\b(?:soc|processor|microprocessor|accelerator|compute (?:chip|die)|"
    r"ai (?:chip|processor|accelerator|engine)|gpu|cpu|server processor)\b",
    re.IGNORECASE,
)
NONLINK_APPLICATION_RE = re.compile(
    r"\b(?:dvd|blu[- ]?ray|optical[- ]disk|pin electronics|"
    r"adc drivers?|led drivers?|gate drivers?|quantum random number generators?|"
    r"qrng|pulsed laser drivers?|network[- ]on[- ]chip|noc|seti|"
    r"reservoir computing|secure proximity|spatial optical modulators?|full hdtv|"
    r"data[- ]link layer|protocol logic|optically clocked.{0,30}samplers?)\b",
    re.IGNORECASE,
)
SECURITY_AUX_RE = re.compile(
    r"\b(?:probing[- ]attack|attack detector|security monitor|secure scan|"
    r"side[- ]channel detector|tamper detector)\b",
    re.IGNORECASE,
)
NONLINK_DIE_RE = re.compile(
    r"\b(?:die[- ]to[- ]die.{0,35}within[- ]die|within[- ]die.{0,35}die[- ]to[- ]die|"
    r"die[- ]to[- ]die (?:variation|error|thermal|process))\b",
    re.IGNORECASE,
)
PHY_RE = re.compile(r"\b(?:phy|physical layer)\b", re.IGNORECASE)
LINK_RATE_SHORTHAND_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s*GT/s|10GBASE[- ]?T|"
    r"(?:10|25|40|50|100|200|400|800)\s*G(?:bE)?|"
    r"(?:10|25|40|50|100|200|400|800)\s*Gbit Ethernet|"
    r"\d+(?:\.\d+)?\s*(?:GB/s|TB/s|Tbit/s))\b",
    re.IGNORECASE,
)
ALGORITHM_METHOD_RE = re.compile(
    r"\b(?:algorithm|theory|experiment|feasibility stud\w*|impact of|"
    r"effect of|performance assessment|experimental assessment|"
    r"performance investigation|methods?|schemes?|techniques?|estimation|"
    r"equalization of|fpga[- ]based|"
    r"software[- ]defined|(?:adaptive|blind|turbo|nonlinear|digital) equaliz\w*)\b",
    re.IGNORECASE,
)
SUPPORT_BLOCK_RE = re.compile(
    r"\b(?:low[- ]dropout regulators?|ldo|dc[- ]dc converters?|buck converters?|"
    r"voltage regulators?)\b",
    re.IGNORECASE,
)
WHOLE_MEMORY_RE = re.compile(
    r"\b(?:v[- ]?nand|nand flash memor(?:y|ies)|"
    r"\d+(?:\.\d+)?\s*(?:Gb|Tb)\s+.{0,28}(?:nand|dram|memory))\b",
    re.IGNORECASE,
)
LINK_CIRCUIT_TECHNIQUE_RE = re.compile(
    r"\b(?:pre[- ]?emphasis|de[- ]?emphasis|toggling serialization|"
    r"source[- ]series terminat\w*|sst drivers?)\b",
    re.IGNORECASE,
)
TRANSMISSION_DEMO_RE = re.compile(
    r"\btransmission\b.{0,55}\b(?:analog\s+)?(?:multiplexers?|mux)\b",
    re.IGNORECASE,
)
DEVICE_LINK_DEMO_RE = re.compile(
    r"\b(?:transferred micro[- ]?leds?|external[- ]source link)\b",
    re.IGNORECASE,
)
LONG_HAUL_OPTICAL_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s*km|long[- ]haul|high[- ]split[- ]ratio pon|"
    r"data[- ]center interconnect|dci|multicore multimode fiber|"
    r"offline digital equaliz\w*)\b",
    re.IGNORECASE,
)

SIGNAL_RULES = [
    ("PAM-4", re.compile(r"\b(?:pam[- ]?4|4[- ]?pam)\b", re.IGNORECASE)),
    ("PAM-8", re.compile(r"\b(?:pam[- ]?8|8[- ]?pam)\b", re.IGNORECASE)),
    ("PAM-3 / Duo", re.compile(r"\b(?:pam[- ]?3|3[- ]?pam|duobinary)\b", re.IGNORECASE)),
    ("NRZ", re.compile(r"\b(?:nrz|non[- ]return[- ]to[- ]zero)\b", re.IGNORECASE)),
]
BLOCK_RULES = [
    ("CDR", re.compile(
        r"clock\s*(?:(?:and|&)\s+)?data recovery|clock[- ]and[- ]data recovery|"
        r"clock recovery|\bcdr\b", re.IGNORECASE,
    )),
    ("Equalizer", re.compile(r"equaliz|\bdfe\b|\bffe\b|\bctle\b", re.IGNORECASE)),
    ("TX", re.compile(r"transmitter|\btx\b|serializer|\bdriver\b|dac[- ]based", re.IGNORECASE)),
    ("RX", re.compile(r"receiver|\brx\b|deserializer|front[- ]end|slicer", re.IGNORECASE)),
    ("System", re.compile(r"serdes|transceiver|serial link|chip[- ]to[- ]chip|die[- ]to[- ]die", re.IGNORECASE)),
]

NUMBER = r"\d+(?:\.\d+)?"
BIT_RATE_UNIT = (
    r"(?:[Tt](?-i:b)(?:/s|ps|it/s)|[Gg](?-i:b)(?:/s|ps|it/s))"
)
SYMBOL_RATE_UNIT = r"(?:g(?:baud|bd))"

MULTILANE_RATE_RE = re.compile(
    rf"(?P<lanes>\d{{1,3}})\s*[x×]\s*(?P<value>{NUMBER})\s*(?:-\s*)?"
    rf"(?P<unit>{BIT_RATE_UNIT})(?P<suffix>\s*/\s*(?:mm(?:\^?2|²)?|pin|wire|lane))?",
    re.IGNORECASE,
)
RANGE_RATE_RE = re.compile(
    rf"(?P<minimum>{NUMBER})\s*(?:-|–|—|to)\s*(?P<maximum>{NUMBER})\s*(?:-\s*)?"
    rf"(?P<unit>{BIT_RATE_UNIT}|{SYMBOL_RATE_UNIT})(?P<suffix>\s*/\s*(?:mm(?:\^?2|²)?|pin|wire|lane))?",
    re.IGNORECASE,
)
RATE_RE = re.compile(
    rf"(?P<value>{NUMBER})\s*(?:-\s*)?(?P<unit>{BIT_RATE_UNIT}|{SYMBOL_RATE_UNIT})"
    rf"(?P<suffix>\s*/\s*(?:mm(?:\^?2|²)?|pin|wire|lane))?",
    re.IGNORECASE,
)
ENERGY_RE = re.compile(
    rf"(?P<value>{NUMBER})\s*(?:-\s*)?(?P<unit>[pf]j)\s*/\s*(?:bit|b)"
    rf"(?P<loss>\s*/\s*dB)?",
    re.IGNORECASE,
)
POWER_PER_RATE_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<value>{NUMBER})\s*(?:-\s*)?"
    rf"(?P<unit>[µμu]W|mW|W)\s*/\s*\(?\s*G(?:b(?:/s)?|bps|bit/s)\s*\)?"
    rf"(?P<loss>\s*/\s*dB)?",
    re.IGNORECASE,
)
POWER_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?P<value>{NUMBER})\s*(?:-\s*)?"
    rf"(?P<unit>[µμu]W|mW|W)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
PROCESS_RE = re.compile(
    rf"(?P<value>{NUMBER})\s*(?:-\s*)?(?P<unit>nm|[µμu]m)\b",
    re.IGNORECASE,
)
DB_RE = re.compile(rf"(?P<value>{NUMBER})\s*(?:-\s*)?dB\b", re.IGNORECASE)
FREQUENCY_RE = re.compile(rf"(?P<value>{NUMBER})\s*(?P<unit>[GMTK])Hz\b", re.IGNORECASE)
BER_RE = re.compile(
    r"(?:\bBER\b|bit[- ]error rate).{0,45}?"
    r"(?P<value>(?:\d+(?:\.\d+)?\s*[x×]\s*)?10\s*\^?\s*-\s*\d+|"
    r"\d+(?:\.\d+)?[eE]-\d+)",
    re.IGNORECASE,
)


def plain_text(value: str | None) -> str:
    """Normalize API/HTML/LaTeX-ish text without changing its numeric meaning."""
    text = unicodedata.normalize("NFKC", unescape(str(value or "")))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\\times", "×")
    # Preserve LaTeX micro-units before the generic command scrubber.  Dropping
    # ``\\mu`` would turn ``36 $\\mu$W`` into ``36 W`` and inflate power 1e6×.
    text = re.sub(r"\\mu\b", "µ", text)
    for _ in range(3):
        text = re.sub(
            r"\\(?:boldsymbol|mathbf|mathrm|text|operatorname)\s*\{([^{}]*)\}",
            r"\1",
            text,
        )
    text = re.sub(r"\\[A-Za-z]+", " ", text)
    text = text.replace("{", "").replace("}", "").replace("$", "")
    text = re.sub(r"[‐‑‒–—―−]", "-", text)
    return " ".join(text.split())


def _medium_implementation_sentence(
    text_value: str | None, pattern: re.Pattern,
) -> str | None:
    """Return an implementation sentence, not a background keyword hit."""
    text = plain_text(text_value)
    if not text:
        return None
    for sentence in re.split(r"(?<=[.!?;])\s+", text):
        if pattern.search(sentence) and LINK_MEDIUM_ABSTRACT_IMPLEMENTATION_RE.search(sentence):
            return sentence[:1000]
    return None


def classify_link_medium(
    title: str | None,
    abstract: str | None = None,
    measurement_link_classes=None,
    screening_class: str | None = None,
) -> dict:
    """Classify the physical link medium with an auditable three-state result.

    ``unspecified`` is intentional: CMOS/BiCMOS, PAM/NRZ, TX/RX, TIA and CDR
    describe implementation or signaling, not whether the channel is optical or
    electrical.  An optical endpoint can therefore remain ``Optical + CMOS``.
    """
    title_text = plain_text(title)
    abstract_text = plain_text(abstract)
    if isinstance(measurement_link_classes, str):
        raw_classes = re.split(r"[,;|\s]+", measurement_link_classes)
    else:
        raw_classes = measurement_link_classes or ()
    classes = {
        str(value or "").strip().lower()
        for value in raw_classes
        if str(value or "").strip()
    }

    title_optical = bool(LINK_MEDIUM_OPTICAL_RE.search(title_text))
    title_electrical = bool(LINK_MEDIUM_ELECTRICAL_RE.search(title_text))
    title_mixed = bool(LINK_MEDIUM_EXPLICIT_MIXED_RE.search(title_text))

    def result(medium, source, confidence, *reasons, evidence=None):
        return {
            "link_medium": medium,
            "medium_source": source,
            "medium_confidence": float(confidence),
            "medium_reason_codes": [reason for reason in reasons if reason],
            "medium_evidence": plain_text(evidence)[:1000] if evidence else None,
            "medium_classifier_version": LINK_MEDIUM_VERSION,
        }

    # A title that explicitly presents both channel types must not be flattened
    # into either population.  Compound descriptions such as "chiplet optical
    # I/O" have no conjunction and correctly remain optical.
    if title_mixed:
        return result(
            "unspecified", "title", 0.45, "explicit_mixed_title",
            "optical_title", "electrical_title", evidence=title_text,
        )
    if title_optical:
        return result(
            "optical", "title", 0.96, "explicit_optical_title",
            evidence=title_text,
        )

    measurement_optical = "optical" in classes
    measurement_electrical = bool(classes & LINK_MEDIUM_MEASUREMENT_ELECTRICAL)
    if measurement_optical and measurement_electrical:
        return result(
            "unspecified", "measurement", 0.50, "mixed_measurement_classes",
            evidence=", ".join(sorted(classes)),
        )
    if measurement_optical:
        return result(
            "optical", "measurement", 0.94, "measurement_optical",
            evidence="optical",
        )
    if title_electrical:
        return result(
            "electrical", "title", 0.95, "explicit_electrical_title",
            evidence=title_text,
        )
    if measurement_electrical:
        return result(
            "electrical", "measurement", 0.90, "measurement_electrical_topology",
            evidence=", ".join(sorted(classes & LINK_MEDIUM_MEASUREMENT_ELECTRICAL)),
        )

    abstract_mixed = bool(LINK_MEDIUM_EXPLICIT_MIXED_RE.search(abstract_text))
    optical_sentence = _medium_implementation_sentence(
        abstract_text, LINK_MEDIUM_OPTICAL_RE,
    )
    electrical_sentence = _medium_implementation_sentence(
        abstract_text, LINK_MEDIUM_ELECTRICAL_RE,
    )
    if abstract_mixed and optical_sentence and electrical_sentence:
        return result(
            "unspecified", "abstract", 0.42, "explicit_mixed_abstract",
            evidence=optical_sentence,
        )
    if optical_sentence:
        return result(
            "optical", "abstract", 0.84, "optical_implementation_abstract",
            evidence=optical_sentence,
        )
    if electrical_sentence:
        return result(
            "electrical", "abstract", 0.82, "electrical_implementation_abstract",
            evidence=electrical_sentence,
        )

    screening_value = str(screening_class or "").strip().lower()
    if screening_value == "core" and LINK_MEDIUM_CORE_AMBIGUOUS_RE.search(title_text):
        return result(
            "unspecified", "screening", 0.35, "core_optical_boundary",
            evidence=title_text,
        )

    # In this survey, direct SerDes/serializer/wireline terminology denotes the
    # conventional electrical channel unless an optical endpoint was explicit.
    if LINK_MEDIUM_GENERIC_ELECTRICAL_RE.search(title_text):
        return result(
            "electrical", "title", 0.78, "serdes_electrical_default",
            evidence=title_text,
        )
    if screening_value == "core":
        return result(
            "electrical", "screening", 0.68, "core_serdes_electrical_default",
            evidence="Core SerDes",
        )
    return result("unspecified", "none", 0.0, "insufficient_medium_evidence")


def normalize_title(value: str | None) -> str:
    text = plain_text(value).lower()
    text = re.sub(r"\b(?:a|an|the)\b", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def title_similarity(left: str | None, right: str | None) -> float:
    """Return a conservative title score combining order and token overlap."""
    a, b = normalize_title(left), normalize_title(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(a.split()), set(b.split())
    union = a_tokens | b_tokens
    jaccard = len(a_tokens & b_tokens) / len(union) if union else 0.0
    return 0.62 * sequence + 0.38 * jaccard


def canonical_ieee_url(article_number: str | int | None, stored_url: str | None = None) -> str:
    key = str(article_number or "").strip()
    if key.isdigit():
        return f"https://ieeexplore.ieee.org/document/{key}/"
    return str(stored_url or "")


def _to_giga(value: float, unit: str) -> float:
    return value * 1000.0 if unit.lower().startswith("t") else value


def _excerpt(text: str, start: int, end: int, radius: int = 150) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    for marker in (". ", "; "):
        boundary = text.rfind(marker, left, start)
        if boundary >= left:
            left = boundary + len(marker)
            break
    for marker in (". ", "; "):
        boundary = text.find(marker, end, right)
        if boundary >= 0:
            right = boundary + 1
            break
    return text[left:right].strip()


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    return any(span[0] < other[1] and other[0] < span[1] for other in occupied)


def _rate_suffix_kind(suffix: str) -> str:
    suffix = re.sub(r"\s+", "", suffix or "").lower()
    if suffix.startswith("/mm"):
        return "throughput_density"
    if suffix == "/pin":
        return "pin_rate_density"
    if suffix in {"/wire", "/lane"}:
        return "bit_rate_per_lane"
    return "bit_rate"


def _candidate(raw: str, value: float | None, excerpt: str, **extra) -> dict:
    result = {"raw": raw, "value": value, "excerpt": excerpt}
    result.update(extra)
    return result


def _parse_ber(value: str) -> float | None:
    compact = re.sub(r"\s+", "", value).lower()
    try:
        if "e-" in compact:
            return float(compact)
        coefficient = 1.0
        if "×" in compact or "x" in compact:
            parts = re.split(r"[x×]", compact, maxsplit=1)
            coefficient = float(parts[0])
            compact = parts[1]
        exponent_match = re.search(r"10\^?-(\d+)", compact)
        if exponent_match:
            return coefficient * (10 ** -int(exponent_match.group(1)))
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def extract_performance(text_value: str | None) -> dict:
    """Extract typed candidates and only unambiguous headline values."""
    text = plain_text(text_value)
    rates: list[dict] = []
    energies: list[dict] = []
    powers: list[dict] = []
    processes: list[dict] = []
    losses: list[dict] = []
    ber_candidates: list[dict] = []
    occupied: list[tuple[int, int]] = []

    for match in MULTILANE_RATE_RE.finditer(text):
        span = match.span()
        occupied.append(span)
        lane_count = int(match.group("lanes"))
        lane_rate = _to_giga(float(match.group("value")), match.group("unit"))
        kind = _rate_suffix_kind(match.group("suffix") or "")
        rates.append(_candidate(
            match.group(0), lane_rate, _excerpt(text, *span), kind=kind,
            lane_count=lane_count, lane_rate_gbps=lane_rate,
            aggregate_rate_gbps=lane_rate * lane_count,
        ))

    for match in RANGE_RATE_RE.finditer(text):
        span = match.span()
        if _overlaps(span, occupied):
            continue
        occupied.append(span)
        minimum = _to_giga(float(match.group("minimum")), match.group("unit"))
        maximum = _to_giga(float(match.group("maximum")), match.group("unit"))
        is_symbol = "baud" in match.group("unit").lower() or match.group("unit").lower().endswith("bd")
        kind = "symbol_rate_range" if is_symbol else _rate_suffix_kind(match.group("suffix") or "") + "_range"
        rates.append(_candidate(
            match.group(0), None, _excerpt(text, *span), kind=kind,
            minimum=min(minimum, maximum), maximum=max(minimum, maximum),
        ))

    for match in RATE_RE.finditer(text):
        span = match.span()
        if _overlaps(span, occupied):
            continue
        occupied.append(span)
        value = _to_giga(float(match.group("value")), match.group("unit"))
        unit = match.group("unit").lower()
        is_symbol = "baud" in unit or unit.endswith("bd")
        kind = "symbol_rate" if is_symbol else _rate_suffix_kind(match.group("suffix") or "")
        context = text[max(0, span[0] - 35):min(len(text), span[1] + 35)].lower()
        scope = "aggregate" if re.search(r"\b(?:aggregate|total)\b", context) else "unknown"
        if kind == "bit_rate_per_lane":
            scope = "lane"
        rates.append(_candidate(
            match.group(0), value, _excerpt(text, *span), kind=kind, rate_scope=scope,
        ))

    energy_spans: list[tuple[int, int]] = []
    for match in ENERGY_RE.finditer(text):
        span = match.span()
        energy_spans.append(span)
        value = float(match.group("value"))
        if match.group("unit").lower().startswith("f"):
            value /= 1000.0
        kind = "loss_normalized_energy" if match.group("loss") else "energy"
        energies.append(_candidate(
            match.group(0), value, _excerpt(text, *span), kind=kind,
        ))

    for match in POWER_PER_RATE_RE.finditer(text):
        span = match.span()
        if _overlaps(span, energy_spans):
            continue
        unit = match.group("unit").lower()
        scale = 0.001 if unit in {"µw", "μw", "uw"} else (1000.0 if unit == "w" else 1.0)
        energy_kind = "loss_normalized_energy" if match.group("loss") else "energy"
        converted_value = float(match.group("value")) * scale
        energies.append(_candidate(
            match.group(0), converted_value, _excerpt(text, *span),
            kind=energy_kind, converted_from=f"{match.group('unit')}/(Gb/s)",
            # Very large W/(Gb/s) values in public metadata often mean a lost
            # micro-prefix.  Keep the text as a review candidate; never guess
            # whether the source meant µW, mW, or W.
            needs_review=(unit == "w" and converted_value > 10000.0),
        ))

    power_per_rate_spans = [match.span() for match in POWER_PER_RATE_RE.finditer(text)]
    for match in POWER_RE.finditer(text):
        span = match.span()
        if any(span[0] >= item[0] and span[1] <= item[1] for item in power_per_rate_spans):
            continue
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit in {"µw", "μw", "uw"}:
            value /= 1000.0
        elif unit == "w":
            value *= 1000.0
        powers.append(_candidate(match.group(0), value, _excerpt(text, *span), kind="power_mw"))

    process_context_re = re.compile(
        r"m?cmos(?![- ]compatible)|finfet|process|technology|fabricat|"
        r"bi[- ]?cmos|sige|fd[- ]?soi|\bsoi\b|\bhbt\b|\binp\b|\bgaas\b",
        re.IGNORECASE,
    )
    optical_dimension_re = re.compile(
        r"wavelength|optical|spectral|bandwidth|c[- ]band|o[- ]band|"
        r"photonic|waveguide|filter",
        re.IGNORECASE,
    )
    for match in PROCESS_RE.finditer(text):
        value = float(match.group("value"))
        if match.group("unit").lower() != "nm":
            value *= 1000.0
        if value > 500:
            continue
        span = match.span()
        context = text[max(0, span[0] - 55):min(len(text), span[1] + 55)]
        has_process_context = bool(process_context_re.search(context))
        bare_in_node = bool(
            re.search(r"\bin\s*$", text[max(0, span[0] - 5):span[0]], re.IGNORECASE)
        )
        if not has_process_context and not (
            bare_in_node and value <= 300 and not optical_dimension_re.search(context)
        ):
            continue
        candidate = _candidate(match.group(0), value, _excerpt(text, *span), kind="process_nm")
        # PDF text can split "40nm" into "4 0nm". Do not turn the trailing
        # fragment (or zero) into a measured process node; retain it for review.
        if value <= 0 or re.search(r"\d\s+$", text[max(0, span[0] - 12):span[0]]):
            candidate["needs_review"] = True
        processes.append(candidate)

    for match in DB_RE.finditer(text):
        span = match.span()
        context = text[max(0, span[0] - 65):min(len(text), span[1] + 65)]
        if not re.search(r"loss|channel|insertion", context, re.IGNORECASE):
            continue
        frequency = None
        frequency_match = FREQUENCY_RE.search(context)
        if frequency_match:
            frequency = float(frequency_match.group("value"))
            multiplier = {"k": 1e-6, "m": 1e-3, "g": 1.0, "t": 1000.0}[frequency_match.group("unit").lower()]
            frequency *= multiplier
        losses.append(_candidate(
            match.group(0), float(match.group("value")), _excerpt(text, *span),
            kind="channel_loss_db", frequency_ghz=frequency,
        ))

    for match in BER_RE.finditer(text):
        value = _parse_ber(match.group("value"))
        if value is None or not (0 < value <= 1):
            continue
        span = match.span()
        context = text[max(0, span[0] - 30):min(len(text), span[1] + 30)].lower()
        scope = "post_fec" if re.search(r"post[- ]?fec", context) else (
            "pre_fec" if re.search(r"pre[- ]?fec", context) else "unknown"
        )
        ber_candidates.append(_candidate(
            match.group("value"), value, _excerpt(text, *span), kind="ber", scope=scope,
        ))

    result = {
        "reported_rate_text": None,
        "reported_rate_gbps": None,
        "reported_rate_min_gbps": None,
        "reported_rate_max_gbps": None,
        "rate_scope": None,
        "lane_rate_gbps": None,
        "lane_count": None,
        "aggregate_rate_gbps": None,
        "symbol_rate_gbaud": None,
        "power_mw": None,
        "energy_pj_bit": None,
        "energy_loss_normalized_pj_bit_db": None,
        "throughput_density_gbps_per_mm": None,
        "process_nm": None,
        "channel_loss_db": None,
        "loss_frequency_ghz": None,
        "ber": None,
        "ber_scope": "unknown",
        "ambiguous_fields": [],
        "candidates": {
            "rates": rates, "energies": energies, "powers": powers,
            "processes": processes, "losses": losses, "ber": ber_candidates,
        },
        "evidence": {},
    }

    bit_rates = [item for item in rates if item["kind"] in {"bit_rate", "bit_rate_per_lane"}]
    multi_rates = [item for item in bit_rates if item.get("lane_count")]
    ranges = [item for item in rates if item["kind"].endswith("_range") and item["kind"].startswith("bit_rate")]
    selected_rate = multi_rates[0] if len(multi_rates) == 1 else (bit_rates[0] if len(bit_rates) == 1 else None)
    if selected_rate:
        result["reported_rate_text"] = selected_rate["raw"]
        result["reported_rate_gbps"] = selected_rate["value"]
        result["rate_scope"] = selected_rate.get("rate_scope") or ("lane" if selected_rate.get("lane_count") else "unknown")
        result["lane_rate_gbps"] = selected_rate.get("lane_rate_gbps")
        result["lane_count"] = selected_rate.get("lane_count")
        result["aggregate_rate_gbps"] = selected_rate.get("aggregate_rate_gbps")
        if result["rate_scope"] == "lane" and result["lane_rate_gbps"] is None:
            result["lane_rate_gbps"] = selected_rate["value"]
        if result["rate_scope"] == "aggregate" and result["aggregate_rate_gbps"] is None:
            result["aggregate_rate_gbps"] = selected_rate["value"]
        result["evidence"]["reported_rate_gbps"] = selected_rate
    elif len(ranges) == 1:
        item = ranges[0]
        result["reported_rate_text"] = item["raw"]
        result["reported_rate_min_gbps"] = item["minimum"]
        result["reported_rate_max_gbps"] = item["maximum"]
        result["rate_scope"] = "unknown"
        result["evidence"]["reported_rate_range"] = item
    elif len(bit_rates) > 1 or len(ranges) > 1:
        result["ambiguous_fields"].append("reported_rate_gbps")

    symbol_rates = [item for item in rates if item["kind"] == "symbol_rate"]
    if len(symbol_rates) == 1:
        result["symbol_rate_gbaud"] = symbol_rates[0]["value"]
        result["evidence"]["symbol_rate_gbaud"] = symbol_rates[0]
    elif len(symbol_rates) > 1:
        result["ambiguous_fields"].append("symbol_rate_gbaud")

    density_rates = [item for item in rates if item["kind"] == "throughput_density"]
    if len(density_rates) == 1:
        result["throughput_density_gbps_per_mm"] = density_rates[0]["value"]
        result["evidence"]["throughput_density_gbps_per_mm"] = density_rates[0]

    normal_energy = [
        item for item in energies
        if item["kind"] == "energy" and not item.get("needs_review")
    ]
    normalized_energy = [
        item for item in energies
        if item["kind"] == "loss_normalized_energy" and not item.get("needs_review")
    ]
    unique_energy = {round(item["value"], 12) for item in normal_energy}
    if len(unique_energy) == 1 and normal_energy:
        result["energy_pj_bit"] = normal_energy[0]["value"]
        result["evidence"]["energy_pj_bit"] = normal_energy[0]
    elif len(unique_energy) > 1:
        result["ambiguous_fields"].append("energy_pj_bit")
    if len(normalized_energy) == 1:
        result["energy_loss_normalized_pj_bit_db"] = normalized_energy[0]["value"]
        result["evidence"]["energy_loss_normalized_pj_bit_db"] = normalized_energy[0]
    if any(item.get("needs_review") and item["kind"] == "energy" for item in energies):
        result["ambiguous_fields"].append("energy_pj_bit")
    if any(
        item.get("needs_review") and item["kind"] == "loss_normalized_energy"
        for item in energies
    ):
        result["ambiguous_fields"].append("energy_loss_normalized_pj_bit_db")

    unique_power = {round(item["value"], 9) for item in powers}
    if len(unique_power) == 1 and powers:
        result["power_mw"] = powers[0]["value"]
        result["evidence"]["power_mw"] = powers[0]
    elif len(unique_power) > 1:
        result["ambiguous_fields"].append("power_mw")

    unique_process = {round(item["value"], 6) for item in processes}
    if any(item.get("needs_review") for item in processes):
        result["ambiguous_fields"].append("process_nm")
    elif len(unique_process) == 1 and processes:
        result["process_nm"] = processes[0]["value"]
        result["evidence"]["process_nm"] = processes[0]
    elif len(unique_process) > 1:
        result["ambiguous_fields"].append("process_nm")

    unique_loss = {round(item["value"], 6) for item in losses}
    if len(unique_loss) == 1 and losses:
        result["channel_loss_db"] = losses[0]["value"]
        result["loss_frequency_ghz"] = losses[0].get("frequency_ghz")
        result["evidence"]["channel_loss_db"] = losses[0]
    elif len(unique_loss) > 1:
        result["ambiguous_fields"].append("channel_loss_db")

    unique_ber = {item["value"] for item in ber_candidates}
    if len(unique_ber) == 1 and ber_candidates:
        result["ber"] = ber_candidates[0]["value"]
        result["ber_scope"] = ber_candidates[0].get("scope") or "unknown"
        result["evidence"]["ber"] = ber_candidates[0]
    elif len(unique_ber) > 1:
        result["ambiguous_fields"].append("ber")
    return result


def taxonomy(text_value: str | None) -> dict:
    text = plain_text(text_value)
    signals = [label for label, pattern in SIGNAL_RULES if pattern.search(text)]
    blocks = [label for label, pattern in BLOCK_RULES if pattern.search(text)]
    performance = extract_performance(text)

    rate_labels = []
    for item in performance["candidates"]["rates"]:
        if item["kind"] in {"throughput_density", "pin_rate_density"}:
            continue
        if item.get("lane_count"):
            label = f"{item['lane_count']}×{item['lane_rate_gbps']:g} Gb/s ({item['aggregate_rate_gbps']:g} aggregate)"
        elif item.get("value") is not None:
            unit = "GBd" if item["kind"] == "symbol_rate" else "Gb/s"
            label = f"{item['value']:g} {unit}"
        else:
            label = item["raw"]
        if label not in rate_labels:
            rate_labels.append(label)

    energy_labels = []
    for item in performance["candidates"]["energies"]:
        if item["kind"] != "energy":
            continue
        label = f"{item['value']:g} pJ/b"
        if label not in energy_labels:
            energy_labels.append(label)

    return {
        "signals": signals or ["Other"],
        "blocks": blocks or ["Unclassified"],
        "rates": rate_labels[:3],
        "energy": energy_labels[:3],
        "performance": performance,
    }


def _has_ic_evidence(text: str) -> bool:
    """Recognize IC processes without treating optical wavelengths as nodes."""
    if IC_TECHNOLOGY_RE.search(text):
        return True
    if IC_ABBREVIATION_RE.search(text) and not PHOTONIC_IC_RE.search(text):
        return True
    for match in PROCESS_NODE_RE.finditer(text):
        nm_text, micron_text = match.groups()
        plausible_value = (
            nm_text is not None and 0.5 <= float(nm_text) <= 500
        ) or (
            micron_text is not None and 0.05 <= float(micron_text) <= 1.0
        )
        if not plausible_value:
            continue
        window = text[max(0, match.start() - 36):match.end() + 36]
        if PROCESS_CONTEXT_RE.search(window):
            return True
    return False


def screen_serdes_relevance(
    title: str | None,
    abstract: str | None = None,
) -> dict:
    """Screen one paper with a transparent, reproducible SerDes rubric.

    The score is only an ordering aid.  The returned class and reason codes are
    retained so venue-level false positives can be audited instead of hidden.
    """
    title_text = plain_text(title)
    abstract_text = plain_text(abstract)
    corpus = f"{title_text}. {abstract_text}".strip()
    features = {
        name: bool(pattern.search(corpus))
        for name, pattern in SCREENING_PATTERNS.items()
    }
    title_features = {
        name: bool(pattern.search(title_text))
        for name, pattern in SCREENING_PATTERNS.items()
    }
    # Keep optical wavelengths such as 850/1310/1550 nm from masquerading as
    # semiconductor process nodes in the screening evidence.
    features["ic_evidence"] = _has_ic_evidence(corpus)
    title_features["ic_evidence"] = _has_ic_evidence(title_text)
    performance = extract_performance(corpus)
    title_performance = extract_performance(title_text)
    has_rate = bool(performance["candidates"]["rates"])
    has_energy = bool(performance["candidates"]["energies"])
    has_metric = has_rate or has_energy or bool(LINK_RATE_SHORTHAND_RE.search(corpus))
    title_has_rate = bool(title_performance["candidates"]["rates"])
    title_has_rate_evidence = title_has_rate or bool(
        LINK_RATE_SHORTHAND_RE.search(title_text)
    )
    title_has_energy_evidence = bool(
        title_performance["candidates"]["energies"]
    ) or title_features["efficiency_evidence"]

    high_speed_tia_title = bool(HIGH_SPEED_TIA_RE.search(title_text)) and bool(
        title_has_rate_evidence
        or TIA_BANDWIDTH_RE.search(title_text)
        or (
            RF_BANDWIDTH_OR_CLOCK_RE.search(title_text)
            and title_features["ic_evidence"]
        )
    )
    integrated_optical_endpoint_title = bool(
        INTEGRATED_OPTICAL_ENDPOINT_RE.search(title_text)
    )
    optical_driver_circuit_title = bool(
        OPTICAL_DRIVER_CIRCUIT_RE.search(title_text)
    )
    optical_link_translator_title = bool(
        OPTICAL_LINK_TRANSLATOR_RE.search(title_text)
    )
    explicit_optical_title = bool(
        OPTICAL_ANCHOR_RE.search(title_text)
    ) or high_speed_tia_title
    carrier_rf_title = bool(RF_CARRIER_TITLE_RE.search(title_text)) and not any((
        RF_BANDWIDTH_OR_CLOCK_RE.search(title_text),
        CLOCKING_FREQUENCY_CONTEXT_RE.search(title_text),
        explicit_optical_title,
    ))
    rf_application_title = bool(RF_APPLICATION_RE.search(title_text))
    optical_modulation_only = (
        explicit_optical_title
        and rf_application_title
        and not EXPLICIT_RF_APPLICATION_RE.search(title_text)
    )
    strong_rf_title = bool(
        (rf_application_title and not optical_modulation_only)
        or RF_NAMED_BAND_RE.search(title_text)
        or RF_OOK_CARRIER_RE.search(title_text)
        or RF_POWER_CARRIER_RE.search(title_text)
        or carrier_rf_title
    )
    rf_application_context = (
        bool(RF_APPLICATION_RE.search(corpus))
        and not DIRECT_LINK_CONTEXT_RE.search(title_text)
    )
    coherent_optical_title = (
        bool(COHERENT_RE.search(title_text))
        and not strong_rf_title
        and not DIRECT_LINK_CONTEXT_RE.search(title_text)
    )
    optical_title = explicit_optical_title or coherent_optical_title
    optical_context = (
        bool(OPTICAL_ANCHOR_RE.search(corpus))
        or coherent_optical_title
        or high_speed_tia_title
    )
    unambiguous_wireline = bool(UNAMBIGUOUS_WIRELINE_RE.search(title_text))
    primary_cdr_title = bool(PRIMARY_CDR_RE.search(title_text))
    explicit_cdr_title = bool(EXPLICIT_CDR_RE.search(title_text))
    equalizer_title = bool(EQUALIZER_RE.search(title_text))
    circuit_equalizer_title = bool(CIRCUIT_EQUALIZER_RE.search(title_text))
    adjacent_block_title = bool(ADJACENT_BLOCK_RE.search(title_text))
    generic_endpoint_block_title = bool(
        GENERIC_ENDPOINT_BLOCK_RE.search(title_text)
    )
    memory_io_title = bool(MEMORY_IO_RE.search(title_text))
    memory_endpoint_block_title = bool(MEMORY_ENDPOINT_BLOCK_RE.search(title_text))
    memory_test_title = bool(MEMORY_TEST_RE.search(title_text))
    whole_memory_title = bool(WHOLE_MEMORY_RE.search(title_text))
    parallel_link_title = bool(PARALLEL_LINK_RE.search(title_text))
    system_platform_title = bool(SYSTEM_PLATFORM_RE.search(title_text))
    security_aux_title = bool(SECURITY_AUX_RE.search(title_text))
    nonlink_application_title = bool(NONLINK_APPLICATION_RE.search(title_text))
    nonlink_die_title = bool(NONLINK_DIE_RE.search(title_text))
    phy_title = bool(PHY_RE.search(title_text))
    algorithm_method_title = bool(ALGORITHM_METHOD_RE.search(title_text))
    support_block_title = bool(SUPPORT_BLOCK_RE.search(title_text))
    direct_serdes_title = bool(DIRECT_LINK_CONTEXT_RE.search(title_text))
    direct_implementation_title = bool(
        DIRECT_IMPLEMENTATION_CONTEXT_RE.search(title_text)
    )
    endpoint_title = bool(PRIMARY_ENDPOINT_RE.search(title_text))
    primary_hardware_title = endpoint_title or primary_cdr_title or (
        equalizer_title and title_has_rate_evidence
    )
    hardware_evidence = primary_hardware_title and any((
        title_has_rate_evidence,
        title_features["electrical_signaling"],
        title_features["ic_evidence"],
    ))
    cdr_hardware = primary_cdr_title and any((
        unambiguous_wireline,
        direct_serdes_title,
        title_has_rate_evidence and any((
            title_features["ic_evidence"],
            title_features["electrical_signaling"],
        )),
        bool(CDR_CIRCUIT_ANCHOR_RE.search(title_text)),
        bool(CDR_CIRCUIT_TITLE_RE.search(title_text)) and title_has_rate_evidence,
    ))
    strong_direct_hardware = unambiguous_wireline and hardware_evidence
    direct_link_hardware = (
        direct_serdes_title
        and any((
            title_has_rate_evidence,
            title_features["electrical_signaling"],
            title_has_energy_evidence,
        ))
        and (not system_platform_title or primary_hardware_title)
        and any((
            primary_hardware_title,
            title_features["ic_evidence"],
            title_features["circuit_block"],
            direct_implementation_title,
        ))
    )
    optical_electronic_evidence = any((
        features["ic_evidence"],
        title_has_energy_evidence,
        high_speed_tia_title,
        optical_driver_circuit_title,
        primary_cdr_title,
        circuit_equalizer_title,
    ))
    optical_endpoint = (
        optical_context
        and any((
            endpoint_title,
            title_features["link_endpoint"],
            high_speed_tia_title,
            optical_driver_circuit_title,
            features["link_endpoint"] and any((
                direct_serdes_title,
                integrated_optical_endpoint_title,
                optical_link_translator_title,
            )),
            primary_cdr_title,
            equalizer_title,
        ))
        and optical_electronic_evidence
        and (
            has_metric
            or title_features["ic_evidence"]
            or direct_serdes_title
            or bool(MONOLITHIC_OPTICAL_RE.search(title_text))
            or integrated_optical_endpoint_title
            or high_speed_tia_title
            or optical_driver_circuit_title
            or (
                features["ic_evidence"]
                and any((
                    features["efficiency_evidence"],
                    MEASURED_IMPLEMENTATION_RE.search(corpus),
                ))
            )
        )
    )
    optical_link_hardware = (
        optical_context
        and bool(OPTICAL_LINK_HARDWARE_RE.search(title_text))
        and (
            has_metric
            or (
                direct_serdes_title
                and any((
                    title_features["ic_evidence"],
                    bool(MONOLITHIC_OPTICAL_RE.search(title_text)),
                ))
            )
        )
        and any((
            features["ic_evidence"],
            title_has_energy_evidence,
            high_speed_tia_title,
            optical_driver_circuit_title,
        ))
    )
    memory_endpoint = (
        memory_io_title
        and not memory_test_title
        and any((
            endpoint_title,
            title_features["circuit_block"],
            phy_title,
            memory_endpoint_block_title,
        ))
        and any((
            title_has_rate_evidence,
            title_has_energy_evidence,
            title_features["electrical_signaling"],
            title_features["ic_evidence"] and memory_endpoint_block_title,
            endpoint_title and parallel_link_title,
        ))
    )
    parallel_endpoint = (
        parallel_link_title
        and direct_serdes_title
        and (title_has_rate_evidence or title_features["electrical_signaling"])
    )
    adjacent_enabling = (
        adjacent_block_title
        and (title_has_rate_evidence or title_features["electrical_signaling"])
        and (
            not generic_endpoint_block_title
            or optical_context
            or direct_serdes_title
            or title_features["electrical_signaling"]
            or (
                title_has_rate_evidence
                and title_features["ic_evidence"]
                and not nonlink_application_title
            )
        )
        and any((
            title_features["ic_evidence"],
            features["efficiency_evidence"],
            optical_context,
            unambiguous_wireline,
            direct_serdes_title,
        ))
    )
    optical_adjacent_reference = (
        optical_context
        and (
            adjacent_block_title
            or optical_driver_circuit_title
        )
        and (
            title_features["ic_evidence"]
            or title_has_rate_evidence
        )
    )
    optical_link_translator = (
        optical_context
        and optical_link_translator_title
        and title_has_rate_evidence
        and optical_electronic_evidence
    )
    electrical_endpoint_hardware = (
        endpoint_title
        and not optical_context
        and not memory_io_title
        and (title_has_rate_evidence or title_features["electrical_signaling"])
    )
    electrical_equalizer_hardware = (
        equalizer_title
        and not optical_context
        and not memory_io_title
        and title_has_rate_evidence
        and any((
            title_features["ic_evidence"],
            features["ic_evidence"],
            title_features["electrical_signaling"],
            endpoint_title,
            unambiguous_wireline,
            direct_serdes_title,
            circuit_equalizer_title,
        ))
    )
    measured_implementation = (
        bool(MEASURED_IMPLEMENTATION_RE.search(corpus))
        and has_rate
        and features["ic_evidence"]
        and not system_platform_title
        and not memory_test_title
        and not (
            title_features["device_physics"]
            and not any((
                endpoint_title,
                adjacent_block_title,
                title_features["ic_evidence"],
                high_speed_tia_title,
                optical_driver_circuit_title,
            ))
        )
        and any((
            direct_serdes_title,
            endpoint_title,
            primary_cdr_title,
            equalizer_title,
            adjacent_block_title,
            title_features["optical_io"],
        ))
    )
    generic_rf_title = bool(re.search(r"\brf\b", title_text, re.IGNORECASE)) and not any((
        optical_context,
        memory_io_title,
        direct_serdes_title,
    ))
    rf_wireless = (
        (
            strong_rf_title
            or rf_application_context
            or generic_rf_title
            or (
                bool(RF_MODE_RE.search(title_text))
                and not memory_io_title
                and not direct_serdes_title
            )
        )
        and any((
            endpoint_title,
            primary_cdr_title,
            title_features["circuit_block"],
            RF_OOK_CARRIER_RE.search(title_text),
        ))
        and not unambiguous_wireline
        and (not optical_context or strong_rf_title)
    )
    sensing_negative = (
        bool(STRONG_SENSING_APPLICATION_RE.search(title_text))
        or (
            title_features["sensing_application"]
            and not any((
                direct_serdes_title,
                memory_io_title,
                cdr_hardware,
                electrical_endpoint_hardware,
                optical_endpoint,
            ))
        )
        or (
            features["sensing_application"]
            and not any((
                unambiguous_wireline,
                direct_serdes_title,
                memory_io_title,
                cdr_hardware,
                electrical_endpoint_hardware,
                optical_endpoint,
                optical_link_hardware,
            ))
        )
    )
    wireless_negative = (
        title_features["nonwireline_link"]
        or (features["nonwireline_link"] and not unambiguous_wireline)
    )
    passive_optical_device = (
        optical_context
        and (
            (
                bool(STRONG_PASSIVE_OPTICAL_RE.search(title_text))
                and not (
                    integrated_optical_endpoint_title
                    or (
                        endpoint_title
                        and title_features["ic_evidence"]
                        and title_has_rate_evidence
                    )
                )
            )
            or
            (
                bool(PASSIVE_OPTICAL_COMPONENT_RE.search(title_text))
                and not (
                    title_has_rate_evidence
                    and any((
                        direct_implementation_title,
                        endpoint_title,
                    ))
                )
            )
            or (
                title_features["device_physics"]
                and not any((
                    endpoint_title,
                    adjacent_block_title,
                    title_features["ic_evidence"],
                    integrated_optical_endpoint_title,
                    optical_link_hardware,
                    optical_link_translator,
                    optical_link_translator_title,
                    direct_serdes_title and features["circuit_block"] and has_metric,
                    bool(OPTICAL_LINK_HARDWARE_RE.search(title_text))
                    and features["circuit_block"]
                    and has_metric,
                ))
            )
        )
    )
    photonic_device_only = (
        optical_context
        and bool(PHOTONIC_DEVICE_PLATFORM_RE.search(title_text))
        and not optical_electronic_evidence
    )
    primary_photonic_device = (
        bool(PRIMARY_PHOTONIC_DEVICE_RE.search(title_text))
        and not any((
            title_features["ic_evidence"],
            high_speed_tia_title,
            optical_driver_circuit_title,
            adjacent_block_title and title_has_energy_evidence,
            primary_cdr_title,
            circuit_equalizer_title,
        ))
    )
    optical_transport_noise = (
        optical_context
        and features["transport_experiment"]
        and not optical_electronic_evidence
        and any((
            features["algorithm_system"],
            LONG_HAUL_OPTICAL_RE.search(corpus),
        ))
    )
    algorithm_study_noise = (
        bool(ALGORITHM_STUDY_RE.search(title_text))
        and title_features["algorithm_system"]
        and not any((
            title_features["ic_evidence"],
            direct_serdes_title,
        ))
    )
    hard_negative = any((
        sensing_negative,
        wireless_negative,
        rf_wireless,
        security_aux_title,
        nonlink_application_title,
        nonlink_die_title,
        whole_memory_title,
        passive_optical_device,
        photonic_device_only,
        primary_photonic_device,
        optical_transport_noise,
        algorithm_study_noise,
        support_block_title and not any((
            unambiguous_wireline,
            direct_serdes_title,
            high_speed_tia_title,
        )),
    ))
    memory_reference_title = memory_io_title and any((
        title_features["review_article"],
        METHOD_REVIEW_RE.search(title_text),
        memory_endpoint_block_title,
    ))
    optical_reference_title = optical_context and any((
        endpoint_title,
        integrated_optical_endpoint_title,
        optical_link_translator_title,
        bool(OPTICAL_LINK_HARDWARE_RE.search(title_text)),
    ))
    positive_related = any((
        unambiguous_wireline,
        direct_serdes_title,
        primary_cdr_title,
        equalizer_title,
        title_features["electrical_signaling"],
        strong_direct_hardware,
        direct_link_hardware,
        electrical_endpoint_hardware,
        electrical_equalizer_hardware,
        optical_endpoint,
        optical_link_hardware,
        optical_link_translator,
        optical_adjacent_reference,
        optical_reference_title,
        adjacent_enabling,
        memory_endpoint,
        memory_reference_title,
    ))
    title_implemented_hardware = any((
        measured_implementation,
        strong_direct_hardware,
        direct_link_hardware and any((
            primary_hardware_title,
            title_features["ic_evidence"],
            title_features["circuit_block"],
        )),
        cdr_hardware,
        electrical_endpoint_hardware,
        electrical_equalizer_hardware,
        memory_endpoint,
        parallel_endpoint,
        adjacent_enabling,
        optical_link_hardware,
        optical_link_translator,
        optical_adjacent_reference,
        optical_endpoint and any((
            title_features["ic_evidence"],
            direct_serdes_title,
            adjacent_block_title,
        )),
    ))
    implemented_hardware = any((
        title_implemented_hardware,
        hardware_evidence,
        optical_endpoint and title_features["optical_io"],
        optical_link_hardware,
        optical_link_translator,
        optical_adjacent_reference,
    ))
    method_review = (
        title_features["review_article"]
        or bool(METHOD_REVIEW_RE.search(title_text))
        or bool(TRANSMISSION_DEMO_RE.search(title_text))
        or bool(DEVICE_LINK_DEMO_RE.search(title_text))
        or (
            algorithm_method_title
            and positive_related
            and not title_implemented_hardware
        )
        or (
            title_features["algorithm_system"]
            and positive_related
            and not title_implemented_hardware
        )
        or (
            features["algorithm_system"]
            and positive_related
            and not implemented_hardware
        )
    )
    transport_only = (
        features["transport_experiment"]
        and optical_context
        and not any((
        strong_direct_hardware,
        direct_link_hardware,
        electrical_endpoint_hardware,
        electrical_equalizer_hardware,
        memory_endpoint,
        parallel_endpoint,
        adjacent_enabling,
        optical_link_hardware,
        optical_link_translator,
        optical_adjacent_reference,
        optical_endpoint and any((
            title_features["ic_evidence"],
            title_features["optical_io"],
            direct_serdes_title,
            adjacent_block_title,
        )),
        ))
    )
    algorithm_only = (
        features["algorithm_system"]
        and not any((
            strong_direct_hardware,
            direct_link_hardware,
            electrical_endpoint_hardware,
            electrical_equalizer_hardware,
            memory_endpoint,
            parallel_endpoint,
            optical_link_hardware,
            optical_link_translator,
            optical_adjacent_reference,
            optical_endpoint and optical_electronic_evidence,
        ))
    )

    if title_features["non_research"]:
        relevance_class = "out_of_scope"
    elif hard_negative:
        relevance_class = "out_of_scope"
    elif method_review:
        relevance_class = "needs_review" if positive_related else "out_of_scope"
    elif measured_implementation and any((
        optical_context,
        memory_io_title,
        parallel_link_title,
        adjacent_enabling and not any((
            direct_serdes_title,
            endpoint_title,
            primary_cdr_title,
            equalizer_title,
        )),
    )):
        relevance_class = "adjacent"
    elif measured_implementation:
        relevance_class = "core"
    elif (transport_only or algorithm_only) and positive_related:
        relevance_class = "needs_review"
    elif optical_link_hardware or optical_link_translator or optical_adjacent_reference:
        relevance_class = "adjacent"
    elif optical_endpoint:
        relevance_class = "adjacent"
    elif memory_endpoint:
        relevance_class = "adjacent"
    elif parallel_endpoint:
        relevance_class = "adjacent"
    elif any((
        strong_direct_hardware,
        direct_link_hardware,
        cdr_hardware,
        electrical_endpoint_hardware,
        electrical_equalizer_hardware,
    )):
        relevance_class = "core"
    elif adjacent_enabling:
        relevance_class = "adjacent"
    elif positive_related:
        relevance_class = "needs_review"
    else:
        relevance_class = "out_of_scope"

    raw_score = 0.0
    weights = {
        "direct_serdes": 40,
        "circuit_block": 18,
        "link_endpoint": 8,
        "electrical_signaling": 15,
        "ic_evidence": 16,
        "efficiency_evidence": 9,
        "optical_io": 8,
    }
    for name, weight in weights.items():
        if features[name]:
            raw_score += weight
    if has_metric:
        raw_score += 6
    if features["transport_experiment"] and transport_only:
        raw_score -= 28
    if (
        features["device_physics"]
        and not features["circuit_block"]
        and not features["ic_evidence"]
    ):
        raw_score -= 22
    if features["algorithm_system"] and algorithm_only:
        raw_score -= 14
    if hard_negative:
        raw_score -= 55
    if title_features["non_research"]:
        raw_score -= 100
    raw_score = max(0.0, min(100.0, raw_score))
    score_ranges = {
        "core": (75.0, 100.0),
        "adjacent": (50.0, 74.999),
        "needs_review": (25.0, 49.999),
        "out_of_scope": (0.0, 24.999),
    }
    score_min, score_max = score_ranges[relevance_class]
    score = score_min + (score_max - score_min) * (raw_score / 100.0)

    reasons = [name for name, present in features.items() if present]
    if has_rate:
        reasons.append("rate_evidence")
    if has_energy:
        reasons.append("energy_evidence")
    if not abstract_text:
        reasons.append("title_only")
    if strong_direct_hardware or direct_link_hardware:
        reasons.append("strong_direct_hardware")
    if measured_implementation:
        reasons.append("measured_implementation")
    if has_metric and not any((
        features["direct_serdes"], features["circuit_block"],
        features["electrical_signaling"], features["ic_evidence"],
    )):
        reasons.append("rate_or_energy_only")

    labels = {
        "direct_serdes": "direct electrical SerDes terminology",
        "circuit_block": "SerDes circuit block",
        "link_endpoint": "link transmitter/receiver endpoint",
        "electrical_signaling": "PAM/NRZ signaling",
        "ic_evidence": "IC process or technology evidence",
        "efficiency_evidence": "power or energy evidence",
        "optical_io": "optical-I/O context",
        "transport_experiment": "optical transport experiment",
        "device_physics": "photonic device emphasis",
        "algorithm_system": "DSP/coding/system emphasis",
        "sensing_application": "sensing/ranging application",
        "nonwireline_link": "wireless/free-space link",
        "rf_carrier": "RF/mm-wave carrier-frequency circuit",
        "non_research": "editorial/correction/front matter",
        "review_article": "review/tutorial/design-method article",
        "rate_evidence": "reported data rate",
        "energy_evidence": "reported energy efficiency",
        "title_only": "abstract unavailable",
        "strong_direct_hardware": "explicit wireline/SerDes hardware evidence",
        "measured_implementation": "measured IC implementation evidence",
        "rate_or_energy_only": "metric-only title match",
    }
    rationale = "; ".join(labels[reason] for reason in reasons if reason in labels)
    return {
        "scope_version": SERDES_SCREENING_VERSION,
        "relevance_class": relevance_class,
        "relevance_score": round(score, 3),
        "evidence_score": round(raw_score, 3),
        "include_in_survey": relevance_class in {"core", "adjacent"},
        "reason_codes": reasons,
        "rationale": rationale or "no SerDes-specific evidence found",
        "screening_source": "title_abstract" if abstract_text else "title_only",
    }

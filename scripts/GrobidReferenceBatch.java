// Project adapter: GROBID CRF raw-reference parsing, with consolidation disabled.
// PDF segmentation is supplied by a separate local parser; this is not a native
// Windows PDF-to-TEI implementation of GROBID's Linux pdfalto pipeline.
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.ArrayList;
import org.grobid.core.data.BiblioItem;
import org.grobid.core.engines.Engine;
import org.grobid.core.factory.GrobidFactory;
import org.grobid.core.main.LibraryLoader;
import org.grobid.core.utilities.GrobidProperties;

class GrobidReferenceBatch {
    public static void main(String[] args) throws Exception {
        if (args.length != 3) throw new IllegalArgumentException("home input output");
        GrobidProperties.setGrobidHome(args[0]);
        GrobidProperties.getInstance();
        LibraryLoader.load();
        Engine engine = GrobidFactory.getInstance().createEngine();
        List<String> inputs = Files.readAllLines(Path.of(args[1]), StandardCharsets.UTF_8);
        // PDF text may contain C0 controls and U+FFFE/U+FFFF, which cannot be
        // serialized in TEI XML. Keep the experiment's
        // original evidence in Python; sanitize only the CRF input strings.
        List<String> safeInputs = new ArrayList<>();
        for (String input : inputs) {
            StringBuilder safe = new StringBuilder();
            for (int point : input.codePoints().toArray()) {
                if ((point >= 0x20 || point == 9 || point == 10 || point == 13)
                        && point != 0xFFFE && point != 0xFFFF) safe.appendCodePoint(point);
            }
            safeInputs.add(safe.toString());
        }
        List<BiblioItem> results;
        try {
            results = engine.processRawReferences(safeInputs, 0);
            if (results == null || results.size() != inputs.size()) throw new IllegalStateException("Row count mismatch");
        } catch (Exception batchFailure) {
            System.err.println("GROBID_BATCH_RETRY " + batchFailure.getClass().getSimpleName());
            results = new ArrayList<>();
            for (int i = 0; i < safeInputs.size(); i++) {
                try {
                    List<BiblioItem> one = engine.processRawReferences(List.of(safeInputs.get(i)), 0);
                    results.add(one == null || one.size() != 1 ? null : one.get(0));
                } catch (Exception itemFailure) {
                    System.err.println("GROBID_ITEM_FAILED " + i + " " + itemFailure.getClass().getSimpleName());
                    results.add(null);
                }
            }
        }
        StringBuilder output = new StringBuilder("<listBibl>\n");
        for (int i = 0; i < results.size(); i++) {
            BiblioItem item = results.get(i);
            output.append(item == null ? "<biblStruct/>" : item.toTEI(i)).append('\n');
        }
        output.append("</listBibl>\n");
        Files.writeString(Path.of(args[2]), output.toString(), StandardCharsets.UTF_8);
        engine.close();
    }
}

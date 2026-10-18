import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const [inputPath, outputPath, sheetName] = process.argv.slice(2);
if (!inputPath || !outputPath || !sheetName) {
  throw new Error("usage: render_workbook.mjs INPUT OUTPUT SHEET");
}
const input = await FileBlob.load(inputPath);
const workbook = await SpreadsheetFile.importXlsx(input);
const summary = await workbook.inspect({
  kind: "workbook,sheet,formula",
  maxChars: 8000,
  tableMaxRows: 5,
  tableMaxCols: 8,
  options: { maxResults: 50 },
});
await fs.mkdir(new URL(".", `file:///${outputPath.replaceAll("\\", "/")}`), {
  recursive: true,
}).catch(() => {});
await fs.writeFile(`${outputPath}.inspect.ndjson`, summary.ndjson, "utf8");
const preview = await workbook.render({
  sheetName,
  range: "A1:O61",
  scale: 1.5,
  format: "png",
});
await fs.writeFile(outputPath, new Uint8Array(await preview.arrayBuffer()));

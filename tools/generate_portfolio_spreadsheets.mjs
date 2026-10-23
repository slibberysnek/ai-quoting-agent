import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputRoot = process.argv[2];
const qaRoot = process.argv[3];
if (!outputRoot || !qaRoot) {
  throw new Error("usage: generate_portfolio_spreadsheets.mjs OUTPUT_ROOT QA_ROOT");
}

const fontFamily = "Arial";

async function buildWorkbook(relativePath, title, sheetName, headers, rows, widths, formats = {}) {
  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add(sheetName);
  sheet.showGridLines = false;
  const lastColumn = String.fromCharCode(64 + headers.length);
  const lastRow = 5 + rows.length;

  sheet.getRange("A2:" + lastColumn + "2").merge();
  sheet.getRange("A2").values = [[title]];
  sheet.getRange("A2").format.font = {
    name: fontFamily, size: 14, bold: true, color: "#000000",
  };
  sheet.getRange("A3:" + lastColumn + "3").format.borders = {
    bottom: { style: "thin", color: "#6B7C93" },
  };
  sheet.getRange("A5:" + lastColumn + lastRow).values = [headers, ...rows];
  sheet.getRange("A5:" + lastColumn + "5").format = {
    fill: "#24435C",
    font: { name: fontFamily, size: 10, bold: true, color: "#FFFFFF" },
    horizontalAlignment: "center",
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9D9D9" },
  };
  const body = sheet.getRange("A6:" + lastColumn + lastRow);
  body.format = {
    font: { name: fontFamily, size: 10, color: "#222222" },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "all", style: "thin", color: "#D9D9D9" },
  };
  widths.forEach((width, index) => {
    sheet.getRangeByIndexes(0, index, lastRow, 1).format.columnWidth = width;
  });
  for (const [column, numberFormat] of Object.entries(formats)) {
    sheet.getRange(column + "6:" + column + lastRow).format.numberFormat = numberFormat;
  }
  sheet.freezePanes.freezeRows(5);
  sheet.tables.add(
    "A5:" + lastColumn + lastRow,
    true,
    sheetName.replace(/[^A-Za-z0-9]/g, "") + "Table",
  );

  workbook.recalculate();
  const inspection = await workbook.inspect({
    kind: "table",
    range: sheetName + "!A2:" + lastColumn + lastRow,
    include: "values,formulas",
    tableMaxRows: 20,
    tableMaxCols: 10,
  });
  const errorScan = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
    options: { useRegex: true, maxResults: 50 },
    summary: "formula errors in " + relativePath,
  });
  if (!inspection.ndjson.includes(headers[0])) {
    throw new Error("Spreadsheet inspection failed for " + relativePath);
  }
  if (errorScan.ndjson && /#REF!|#DIV\/0!|#VALUE!|#NAME\?|#NUM!|#NULL!|#SPILL!|#CALC!/.test(errorScan.ndjson)) {
    throw new Error("Spreadsheet formula error in " + relativePath);
  }

  const preview = await workbook.render({
    sheetName, autoCrop: "all", scale: 1.5, format: "png",
  });
  const qaPath = path.join(qaRoot, relativePath.replace(/\.xlsx$/i, ".png"));
  await fs.mkdir(path.dirname(qaPath), { recursive: true });
  await fs.writeFile(qaPath, new Uint8Array(await preview.arrayBuffer()));

  const target = path.join(outputRoot, relativePath);
  await fs.mkdir(path.dirname(target), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(target);
}

await buildWorkbook(
  "02_inbound_quote/attachments/rfq_003_store_restock.xlsx",
  "Request for Quote Store Restock",
  "Requested Items",
  ["Requested product", "Quantity", "Unit", "Required by", "Notes"],
  [
    ["USB food flash drive - banana", 120, "Each", new Date("2016-06-24T00:00:00Z"), ""],
    ["USB food flash drive - chocolate bar", 80, "Each", new Date("2016-06-24T00:00:00Z"), "Retail display"],
    ["USB food flash drive - dessert 10 drive variety pack", 12, "Packet", new Date("2016-06-24T00:00:00Z"), "Confirm pack contents"],
  ],
  [42, 12, 12, 15, 25],
  { B: "#,##0", D: "mm/dd/yy" },
);

await buildWorkbook(
  "02_inbound_quote/attachments/rfq_006_mixed_units.xlsx",
  "Request for Quote Mixed Units",
  "Mixed Units",
  ["Customer description", "Quantity", "Unit", "Catalog hint", "Notes"],
  [
    ["brown shipping carton 305 mm", 20, "Outer", "SKU 184", "25 each per outer"],
    ["fragile tape 48x75 black orange", 6, "Outer", "SKU 191", "36 each per outer"],
    ["red tape dispenser", 15, "Each", "SKU 204", ""],
  ],
  [42, 12, 12, 18, 28],
  { B: "#,##0" },
);

await buildWorkbook(
  "04_supplier_delay/attachments/po_2073_split_delivery_schedule.xlsx",
  "Purchase Order 2073 Revised Delivery Schedule",
  "Delivery Schedule",
  ["PO line", "Stock item", "Shipment", "Promised date", "Quantity outer", "Status"],
  [
    [8359, 77, 1, new Date("2016-06-27T00:00:00Z"), 1700, "Confirmed"],
    [8359, 77, 2, new Date("2016-07-05T00:00:00Z"), 1701, "Confirmed"],
    [8360, 78, 1, new Date("2016-06-27T00:00:00Z"), 1800, "Confirmed"],
    [8360, 78, 2, new Date("2016-07-05T00:00:00Z"), 1867, "Confirmed"],
    [8362, 86, 1, new Date("2016-07-05T00:00:00Z"), 3400, "Confirmed"],
    [8362, 86, 2, new Date("2016-07-12T00:00:00Z"), 368, "Unconfirmed"],
  ],
  [12, 14, 12, 18, 18, 18],
  { A: "0", B: "0", C: "0", D: "mm/dd/yy", E: "#,##0" },
);

console.log("Created and verified 3 spreadsheet fixtures");

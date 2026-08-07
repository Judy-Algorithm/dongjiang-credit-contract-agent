import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "file:///C:/Users/18321/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/@oai/artifact-tool/dist/artifact_tool.mjs";

const outputDir = process.argv[2] || "系统完整测试资料";
await fs.mkdir(outputDir, { recursive: true });
const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Contract Clauses");
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(4);

sheet.getRange("A1:D1").merge();
sheet.getRange("A1").values = [["COMPONENT SALES CONTRACT - ENGLISH ATTACHMENT"]];
sheet.getRange("A2:D2").merge();
sheet.getRange("A2").values = [["Demo file for Excel parsing, language recognition, redaction and contract exception review"]];
sheet.getRange("A4:D4").values = [["No.", "Clause", "Contract text", "Review note"]];

const rows = [
  [1, "Parties", "Buyer: Atlas Mobility Systems Ltd. and its subsidiary Atlas Components Ltd. Seller: Dongjiang Precision Manufacturing Co., Ltd.", "Sensitive party names"],
  [2, "Contact", "Contact: Alice Wong; phone +86 13900139000; email alice.wong@example.com.", "Phone and email"],
  [3, "Subject", "Products: precision injection-moulded components. Technical parameters: PPS-GF40; drawing number: DJ-ENG-2026-018.", "Technical parameters"],
  [4, "Contract Value", "Contract amount: USD 520,000.", "Sensitive amount"],
  [5, "Credit", "Credit limit: USD 480,000.", "Above the main-case approved amount"],
  [6, "Payment", "Payment term: Net 120 days after acceptance and receipt of a valid invoice.", "Triggers contract exception authorization"],
  [7, "Delivery", "Delivery follows accepted purchase orders; title and risk pass on customer acceptance.", ""],
  [8, "Liability", "Each party is liable only for proven direct loss. Aggregate liability shall not exceed 30% of the contract amount.", ""],
  [9, "Intellectual Property", "Each party retains background intellectual property. Any licence is solely for performance of this contract, royalty-free, for the contract term and non-transferable.", ""],
  [10, "Confidentiality", "Each party shall protect confidential information, prices, technical parameters and customer data.", ""],
  [11, "Termination", "A material breach not cured within 30 days after written notice permits termination.", ""],
  [12, "Dispute", "This contract is governed by PRC law. Disputes shall be submitted to Shenzhen Court of International Arbitration.", ""],
  [13, "Bank Account", "Settlement account: 6222029876543210123 (demo data only).", "Sensitive bank account"],
];
sheet.getRange(`A5:D${4 + rows.length}`).values = rows;

sheet.getRange("A1:D1").format = {
  fill: "#173D4D",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
sheet.getRange("A2:D2").format = {
  fill: "#E8EEF0",
  font: { color: "#4E5F67", italic: true, size: 9 },
  horizontalAlignment: "center",
};
sheet.getRange("A4:D4").format = {
  fill: "#2D5968",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  borders: { preset: "outside", style: "thin", color: "#173D4D" },
};
sheet.getRange(`A5:D${4 + rows.length}`).format = {
  verticalAlignment: "top",
  wrapText: true,
  borders: { preset: "inside", style: "thin", color: "#DCE3E6" },
};
sheet.getRange(`A5:A${4 + rows.length}`).format = { horizontalAlignment: "center", fill: "#F2F6F7" };
sheet.getRange(`B5:B${4 + rows.length}`).format = { font: { bold: true, color: "#173D4D" }, fill: "#F8FAFA" };
sheet.getRange(`D5:D${4 + rows.length}`).format = { font: { color: "#6B4A20", size: 9 }, fill: "#FFF9ED" };
sheet.getRange("A:A").format.columnWidth = 7;
sheet.getRange("B:B").format.columnWidth = 22;
sheet.getRange("C:C").format.columnWidth = 78;
sheet.getRange("D:D").format.columnWidth = 30;
sheet.getRange("1:1").format.rowHeight = 32;
sheet.getRange("2:2").format.rowHeight = 24;
sheet.getRange("4:4").format.rowHeight = 26;
sheet.getRange(`5:${4 + rows.length}`).format.rowHeight = 38;

const preview = await workbook.render({ sheetName: "Contract Clauses", autoCrop: "all", scale: 1, format: "png" });
await fs.writeFile(path.join(outputDir, "13-四角色主案例-英文合同-preview.png"), new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDir, "13-四角色主案例-英文合同.xlsx"));
const inspection = await workbook.inspect({ kind: "region", sheetId: "Contract Clauses", range: "A1:D17", maxChars: 6000 });
await fs.writeFile(path.join(outputDir, "13-四角色主案例-英文合同-inspect.txt"), inspection.ndjson || String(inspection), "utf8");

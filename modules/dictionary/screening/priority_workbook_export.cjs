const fs = require("node:fs");
const path = require("node:path");
const { SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const [manifestPath, progressPath, outputPath, subject, renderDir = ""] = process.argv.slice(2);
if (!manifestPath || !progressPath || !outputPath || !subject) {
  throw new Error("用法: node priority_workbook_export.cjs <manifest> <progress> <output> <subject> [render-dir]");
}

const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const completed = new Map();
for (const line of fs.readFileSync(progressPath, "utf8").split(/\r?\n/)) {
  if (!line.trim()) continue;
  const item = JSON.parse(line);
  if (item.ok) completed.set(item.source_path, item);
}
const results = manifest.records.map((record) => completed.get(record.source_path)).filter(Boolean);
const records = new Map(manifest.records.map((record) => [record.source_path, record]));
const severityRank = { minor: 1, major: 2, critical: 3 };
const unique = (values) => [...new Set(values.filter(Boolean))];
const cleanSnippet = (text) => String(text || "")
  .replace(/[A-Za-z0-9+/=]{180,}/g, (value) => `${value.slice(0, 20)}【连续编码内容已压缩，共${value.length}字符】`)
  .replace(/(.{1,16}?)\1{8,}/gs, (value, unit) => `${unit.slice(0, 12)}【重复内容已压缩，共${value.length}字符】`)
  .trim();
const isFocus = (problem) => ["major", "critical"].includes(problem.severity) && problem.problem_type !== "年代风险";
const choosePassSamples = (samples) => {
  if (samples.length <= 2) return samples;
  const indexes = unique([
    Math.round((samples.length - 1) / 3),
    Math.round(2 * (samples.length - 1) / 3),
  ]);
  return indexes.map((index) => samples[index]).filter(Boolean);
};

const bookRows = [];
const issueRows = [];
for (const item of results) {
  const result = item.result || {};
  const record = records.get(item.source_path);
  if (!record) continue;
  const title = result.title_guess || item.input_title || record.title || item.file_name;
  if (result.decision === "PASS") {
    const samples = choosePassSamples(record.samples || []);
    const reviewNote = "抽查代表性段落，确认文本语义、术语对应和 OCR 质量与 PASS 结论一致。";
    bookRows.push(["PASS", subject, title, samples.length, result.summary || "", reviewNote, item.source_path]);
    for (const sample of samples) {
      issueRows.push([
        "PASS",
        subject,
        title,
        "PASS抽检",
        "-",
        "不适用",
        "模型未发现需要降级的明确问题，列出代表性段落供人工快速抽检。",
        cleanSnippet(sample.text || "[空白抽样段落]"),
        `样本${sample.sample_no}，约从第${sample.line_start || "?"}行开始`,
        reviewNote,
        item.source_path,
      ]);
    }
    continue;
  }
  if (!["REVIEW", "DROP"].includes(result.decision)) continue;
  const focused = (result.problems || []).filter(isFocus);
  if (!focused.length) continue;
  const grouped = new Map();
  for (const problem of focused) {
    const sampleNo = Number(problem.sample_no) || 1;
    if (!grouped.has(sampleNo)) grouped.set(sampleNo, []);
    grouped.get(sampleNo).push(problem);
  }
  const bookTargets = unique((result.manual_review_targets || []).map((target) => target.reason));
  bookRows.push([result.decision, subject, title, grouped.size, result.summary || "", bookTargets.join("\n"), item.source_path]);
  for (const [sampleNo, problems] of [...grouped.entries()].sort((a, b) => a[0] - b[0])) {
    const sample = (record.samples || []).find((value) => Number(value.sample_no) === sampleNo);
    const targets = (result.manual_review_targets || []).filter((target) => Number(target.sample_no) === sampleNo);
    const severity = problems.reduce(
      (best, problem) => severityRank[problem.severity] > severityRank[best] ? problem.severity : best,
      "minor",
    );
    issueRows.push([
      result.decision,
      subject,
      title,
      unique(problems.map((problem) => problem.problem_type)).join("；"),
      severity,
      problems.every((problem) => problem.rule_cleanable === true) ? "是" : "否",
      problems.map((problem, index) => `${index + 1}. ${problem.anchor ? `“${problem.anchor}”：` : ""}${problem.reason}`).join("\n"),
      cleanSnippet(sample?.text || "[未找到对应抽样段落]"),
      `样本${sampleNo}，约从第${sample?.line_start || "?"}行开始`,
      unique(targets.map((target) => target.reason)).join("\n") || bookTargets.join("\n"),
      item.source_path,
    ]);
  }
}

const workbook = Workbook.create();
const overview = workbook.worksheets.add("总览");
const books = workbook.worksheets.add("重点书目");
const issues = workbook.worksheets.add("审核重点");
const notes = workbook.worksheets.add("说明");
const dark = "#24303F";
const teal = "#3C6670";
const lineColor = "#D9E0E6";
const passFill = "#D9EAD3";
const reviewFill = "#FFF2CC";
const dropFill = "#F4CCCC";
const decisionFill = (decision) => decision === "DROP" ? dropFill : decision === "PASS" ? passFill : reviewFill;
for (const sheet of [overview, books, issues, notes]) sheet.showGridLines = false;

overview.mergeCells("A1:F2");
overview.getRange("A1").values = [[`${subject}工具书重点问题审核表`]];
overview.getRange("A1:F2").format = { fill: dark, font: { bold: true, color: "#FFFFFF", size: 16, name: "Microsoft YaHei" }, horizontalAlignment: "center", verticalAlignment: "center" };
overview.getRange("A4:B7").values = [
  ["模型完成书目", results.length],
  ["REVIEW/DROP书目", results.filter((item) => ["REVIEW", "DROP"].includes(item.result.decision)).length],
  ["工作簿收录书目", bookRows.length],
  ["工作簿收录段落", issueRows.length],
];
overview.getRange("A4:A7").format = { fill: "#DCE6F1", font: { bold: true } };
overview.getRange("B4:B7").format = { font: { bold: true, color: dark }, numberFormat: "#,##0" };
overview.mergeCells("B8:F8");
overview.getRange("A8:F8").values = [["筛选口径", "PASS 每本收录2个分布较开的代表性片段；REVIEW/DROP 收录 major、critical 且有实质性非年代问题的段落。", null, null, null, null]];
overview.getRange("A8:F8").format = { fill: "#EAF2F4", wrapText: true, verticalAlignment: "center" };
overview.getRange("A8").format.font = { bold: true };
overview.getRange("A10:B13").values = [["分级", "收录书目"], ["DROP", bookRows.filter((row) => row[0] === "DROP").length], ["REVIEW", bookRows.filter((row) => row[0] === "REVIEW").length], ["PASS", bookRows.filter((row) => row[0] === "PASS").length]];
overview.getRange("A10:B10").format = { fill: dark, font: { bold: true, color: "#FFFFFF" } };
overview.getRange("A11:B11").format.fill = dropFill;
overview.getRange("A12:B12").format.fill = reviewFill;
overview.getRange("A13:B13").format.fill = passFill;
overview.getRange("A4:B13").format.borders = { preset: "inside", style: "thin", color: lineColor };
overview.getRange("A1:F13").format.font = { name: "Microsoft YaHei" };
overview.getRange("A:A").format.columnWidth = 20;
overview.getRange("B:E").format.columnWidth = 17;
overview.getRange("F:F").format.columnWidth = 24;
overview.getRange("8:8").format.rowHeight = 45;

const bookHeaders = ["分级", "学科", "书名", "重点段落数", "模型问题总结", "人工复查建议", "原MD路径"];
books.getRangeByIndexes(0, 0, 1, bookHeaders.length).values = [bookHeaders];
if (bookRows.length) books.getRangeByIndexes(1, 0, bookRows.length, bookHeaders.length).values = bookRows;
books.getRange(`A1:G${Math.max(1, bookRows.length + 1)}`).format = { font: { name: "Microsoft YaHei", size: 10 }, verticalAlignment: "top", wrapText: true };
books.getRange("A1:G1").format = { fill: dark, font: { bold: true, color: "#FFFFFF", name: "Microsoft YaHei" }, wrapText: true };
for (let index = 0; index < bookRows.length; index++) {
  books.getRange(`A${index + 2}:G${index + 2}`).format.fill = decisionFill(bookRows[index][0]);
  books.getRange(`${index + 2}:${index + 2}`).format.rowHeight = 72;
}
for (const [column, width] of [["A:A", 11], ["B:B", 14], ["C:C", 30], ["D:D", 12], ["E:E", 54], ["F:F", 44], ["G:G", 58]]) books.getRange(column).format.columnWidth = width;
books.freezePanes.freezeRows(1);
if (bookRows.length) books.tables.add(`A1:G${bookRows.length + 1}`, true, "PriorityBooksTable").style = "TableStyleMedium2";

const issueHeaders = ["分级", "学科", "书名", "问题类型", "严重程度", "可规则清洗", "问题描述", "对应MD段落", "段落位置", "人工复查要点", "原MD路径"];
issues.getRangeByIndexes(0, 0, 1, issueHeaders.length).values = [issueHeaders];
if (issueRows.length) issues.getRangeByIndexes(1, 0, issueRows.length, issueHeaders.length).values = issueRows;
issues.getRange(`A1:K${Math.max(1, issueRows.length + 1)}`).format = { font: { name: "Microsoft YaHei", size: 10 }, verticalAlignment: "top", wrapText: true };
issues.getRange("A1:K1").format = { fill: dark, font: { bold: true, color: "#FFFFFF", name: "Microsoft YaHei" }, wrapText: true };
for (let index = 0; index < issueRows.length; index++) {
  issues.getRange(`A${index + 2}:K${index + 2}`).format.fill = decisionFill(issueRows[index][0]);
  issues.getRange(`${index + 2}:${index + 2}`).format.rowHeight = 90;
}
const issueWidths = [11, 14, 30, 20, 12, 13, 48, 70, 24, 44, 58];
for (let index = 0; index < issueWidths.length; index++) issues.getRangeByIndexes(0, index, Math.max(1, issueRows.length + 1), 1).format.columnWidth = issueWidths[index];
issues.freezePanes.freezeRows(1);
if (issueRows.length) issues.tables.add(`A1:K${issueRows.length + 1}`, true, "PriorityIssuesTable").style = "TableStyleMedium2";

notes.mergeCells("A1:F2");
notes.getRange("A1").values = [[`${subject}审核表使用说明`]];
notes.getRange("A1:F2").format = { fill: teal, font: { bold: true, color: "#FFFFFF", size: 15, name: "Microsoft YaHei" }, horizontalAlignment: "center", verticalAlignment: "center" };
notes.getRange("A4:B9").values = [
  ["审核范围", `本文件包含${subject}的 PASS 代表性抽检段落，以及 REVIEW/DROP 的重点问题段落。`],
  ["段落来源", "“对应MD段落”取自模型实际阅读的均匀分布抽样片段；具体抽样数量与长度以运行参数或配置为准。"],
  ["排序", "按短编号顺序排列；同一本书的问题段落连续出现，不插入额外书籍ID分隔行。"],
  ["压缩处理", "连续编码内容和机械重复内容在表中压缩显示；完整原文可通过最后一列的 MD 路径查看。"],
  ["人工复查", "优先检查 DROP，其次检查 REVIEW；PASS 用于快速抽检模型放行是否可信，必要时再回查原 PDF。"],
  ["注意", "模型没有查看 PDF。本表用于缩小人工审核范围，不应把模型结论直接当作最终剔除决定。"],
];
notes.getRange("A4:A9").format = { fill: "#DCE6F1", font: { bold: true, name: "Microsoft YaHei" }, verticalAlignment: "top" };
notes.getRange("B4:B9").format = { wrapText: true, font: { name: "Microsoft YaHei" }, verticalAlignment: "top" };
notes.getRange("A4:B9").format.borders = { preset: "inside", style: "thin", color: lineColor };
notes.getRange("A:A").format.columnWidth = 18;
notes.getRange("B:B").format.columnWidth = 100;
for (let row = 4; row <= 9; row++) notes.getRange(`${row}:${row}`).format.rowHeight = 54;

async function main() {
  if (renderDir) {
    fs.mkdirSync(renderDir, { recursive: true });
    for (const [sheetName, range] of [["总览", "A1:F13"], ["重点书目", `A1:G${Math.min(bookRows.length + 1, 12)}`], ["审核重点", `A1:K${Math.min(issueRows.length + 1, 8)}`], ["说明", "A1:F9"]]) {
      const preview = await workbook.render({ sheetName, range, scale: 1, format: "png" });
      fs.writeFileSync(path.join(renderDir, `${sheetName}.png`), new Uint8Array(await preview.arrayBuffer()));
    }
  }
  const errors = await workbook.inspect({ kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options: { useRegex: true, maxResults: 100 }, summary: "final formula error scan" });
  if (errors.ndjson.includes('"matchCount":') && !errors.ndjson.includes('"matchCount":0')) throw new Error(`工作簿公式错误: ${errors.ndjson}`);
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const output = await SpreadsheetFile.exportXlsx(workbook);
  await output.save(outputPath);
  const inspectPath = `${outputPath}.inspect.ndjson`;
  if (fs.existsSync(inspectPath)) fs.unlinkSync(inspectPath);
  console.log(JSON.stringify({
    output_path: outputPath,
    completed: results.length,
    review_drop_books: results.filter((item) => ["REVIEW", "DROP"].includes(item.result.decision)).length,
    priority_books: bookRows.length,
    priority_issues: issueRows.length,
    priority_drop: bookRows.filter((row) => row[0] === "DROP").length,
    priority_review: bookRows.filter((row) => row[0] === "REVIEW").length,
    priority_pass: bookRows.filter((row) => row[0] === "PASS").length,
  }));
}

main().catch((error) => { console.error(error.stack || String(error)); process.exitCode = 1; });

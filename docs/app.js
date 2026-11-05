const CRM = [
  {id: "c_101", full_name: "Maya Chen", email_address: "MAYA@trailhead.io", company: "Trailhead", lifecycle: "customer", updated: "2026-10-20"},
  {id: "c_102", full_name: "Luis Ortega", email_address: "luis@ridgeline.co", company: "Ridgeline", lifecycle: "lead", updated: "2026-10-22"},
  {id: "c_103", full_name: "Priya Rao", email_address: "priya@summit.dev", company: "Summit", lifecycle: "opportunity", updated: "2026-10-25"},
  {id: "c_104", full_name: "Sam Okafor", email_address: "sam@basecamp.net", company: "Basecamp", lifecycle: "customer", updated: "2026-10-26"},
  {id: "c_105", full_name: "Noah Blake", email_address: "Noah@example.com", company: "Example", lifecycle: "", updated: "2026-10-27"},
];
const CSV = `Contact ID,Name,E-mail,Account,Stage,Phone,Score
csv_9,Jordan Lee,jordan@pinecrest.com,Pinecrest,Lead,555-0101,3
csv_10,Maya Chen,maya@trailhead.io,Trailhead,Customer,555-0102,8
csv_11,Ana Silva,ana@harbor.io,Harbor,Opportunity,555-0103,5
csv_12,Riley Cho,RILEY@northwind.io,Northwind,Lead,,n/a`;
const CRM_SPEC = {
  name: "crm_contacts",
  fields: [
    {target: "email", compute: "lower(strip(email_address))", required: true},
    {target: "name", from: "full_name", cast: "str", required: true},
    {target: "account", from: "company", cast: "str", required: true},
    {target: "stage", compute: "lower(coalesce(lifecycle, 'lead'))"},
    {target: "phone", from: "phone", default: ""},
    {target: "score", from: "lead_score", cast: "int", default: 0},
    {target: "label", compute: "concat(name, ' <', email, '>')"},
  ],
};
const CSV_SPEC = {
  name: "csv_contacts",
  fields: [
    {target: "email", compute: "lower(strip(row['E-mail']))", required: true},
    {target: "name", from: "Name", cast: "str", required: true},
    {target: "account", from: "Account", cast: "str", required: true},
    {target: "stage", compute: "lower(coalesce(row['Stage'], 'lead'))"},
    {target: "phone", from: "Phone", default: ""},
    {target: "score", from: "Score", cast: "int", required: true},
    {target: "label", compute: "concat(name, ' <', email, '>')"},
  ],
};
const FUNCS = new Set(["lower", "strip", "concat", "coalesce"]);

function isBlank(value) {
  return value == null || (typeof value === "string" && value.trim() === "");
}
function yamlScalar(value) {
  if (typeof value === "string") return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) ? value : JSON.stringify(value);
  return String(value);
}
function toYaml(spec) {
  const lines = [`name: ${spec.name}`, "fields:"];
  for (const field of spec.fields) {
    lines.push(`  ${field.target}:`);
    if (field.from) lines.push(`    from: ${yamlScalar(field.from)}`);
    if (field.compute) lines.push(`    compute: ${JSON.stringify(field.compute)}`);
    if (field.cast) lines.push(`    cast: ${field.cast}`);
    if ("default" in field) lines.push(`    default: ${yamlScalar(field.default)}`);
    if (field.required) lines.push("    required: true");
  }
  return lines.join("\n");
}
function parseExpr(src) {
  let i = 0;
  const skip = () => { while (src[i] === " " || src[i] === "\n") i++; };
  function parseIdent() {
    const start = i;
    while (/[A-Za-z0-9_]/.test(src[i] || "")) i++;
    return src.slice(start, i);
  }
  function parseString() {
    const quote = src[i++];
    let out = "";
    while (src[i] && src[i] !== quote) {
      out += src[i] === "\\" ? src[++i] || "" : src[i];
      i++;
    }
    if (src[i] !== quote) throw new Error("unterminated string");
    i++;
    return out;
  }
  function parse() {
    skip();
    if (src[i] === "'" || src[i] === '"') return {type: "str", value: parseString()};
    if (!/[A-Za-z_]/.test(src[i] || "")) throw new Error(`invalid compute expression: ${src}`);
    const name = parseIdent();
    skip();
    if (src[i] === "(") {
      i++;
      const args = [];
      skip();
      if (src[i] !== ")") {
        args.push(parse());
        skip();
        while (src[i] === ",") { i++; args.push(parse()); skip(); }
      }
      if (src[i] !== ")") throw new Error("expected )");
      i++;
      return {type: "call", name, args};
    }
    if (name === "row" && src[i] === "[") {
      i++;
      skip();
      const key = parseString();
      skip();
      if (src[i] !== "]") throw new Error("expected ]");
      i++;
      return {type: "index", key};
    }
    return {type: "name", name};
  }
  const ast = parse();
  skip();
  if (i !== src.length) throw new Error(`invalid compute expression: ${src}`);
  return ast;
}
function exprKeys(ast, keys) {
  if (ast.type === "name" && ast.name !== "row" && !FUNCS.has(ast.name)) keys.add(ast.name);
  if (ast.type === "index") keys.add(ast.key);
  if (ast.type === "call") ast.args.forEach(arg => exprKeys(arg, keys));
  return keys;
}
function consumedKeys(spec) {
  const keys = new Set();
  for (const field of spec.fields) {
    if (field.from) keys.add(field.from.split(".")[0]);
    if (field.compute) exprKeys(parseExpr(field.compute), keys);
  }
  return keys;
}
function evalExpr(ast, env) {
  if (ast.type === "str") return ast.value;
  if (ast.type === "name") {
    if (!Object.prototype.hasOwnProperty.call(env, ast.name)) throw new Error(`unknown field '${ast.name}'`);
    return env[ast.name];
  }
  if (ast.type === "index") {
    if (!Object.prototype.hasOwnProperty.call(env.row, ast.key)) throw new Error(`unknown field '${ast.key}'`);
    return env.row[ast.key];
  }
  const args = ast.args.map(arg => evalExpr(arg, env));
  if (ast.name === "lower") {
    if (typeof args[0] !== "string") throw new Error("lower() expects a string");
    return args[0].toLowerCase();
  }
  if (ast.name === "strip") {
    if (typeof args[0] !== "string") throw new Error("strip() expects a string");
    return args[0].trim();
  }
  if (ast.name === "concat") return args.map(value => (value == null ? "" : String(value))).join("");
  if (ast.name === "coalesce") {
    for (const value of args) if (!isBlank(value)) return value;
    return null;
  }
  throw new Error(`unknown function '${ast.name}'`);
}
function lookup(row, path) {
  let current = row;
  for (const part of path.split(".")) {
    if (current != null && typeof current === "object" && Object.prototype.hasOwnProperty.call(current, part)) {
      current = current[part];
    } else return {found: false, value: null};
  }
  return {found: true, value: current};
}
function castValue(value, kind) {
  if (value == null || (kind !== "str" && typeof value === "string" && value.trim() === "")) return null;
  if (kind === "str") return typeof value === "string" ? value : String(value);
  if (kind === "int") {
    if (typeof value === "number" && Number.isInteger(value)) return value;
    if (typeof value === "string" && /^[+-]?\d+$/.test(value.trim())) return Number(value.trim());
    throw new Error(`cannot cast '${value}' to int`);
  }
  throw new Error(`cannot cast '${value}' to ${kind}`);
}
function mapRecord(row, spec) {
  const env = {row};
  for (const [key, value] of Object.entries(row)) {
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(key) && key !== "row") env[key] = value;
  }
  const changes = [];
  const errors = [];
  const failed = new Set();
  const output = {};
  for (const field of spec.fields) {
    const refs = field.compute ? exprKeys(parseExpr(field.compute), new Set()) : [];
    if ([...refs].some(ref => failed.has(ref))) continue;
    try {
      let value;
      let op;
      let before;
      if (field.compute) {
        value = evalExpr(parseExpr(field.compute), env);
        before = null;
        op = "compute";
        if (field.cast && !isBlank(value)) value = castValue(value, field.cast);
        else if (field.cast && isBlank(value)) value = null;
        if (isBlank(value) && "default" in field) {
          value = field.default;
          op = "default";
        }
      } else {
        const got = lookup(row, field.from || field.target);
        before = got.found ? got.value : null;
        if (!got.found || isBlank(got.value)) {
          if ("default" in field) {
            value = "cast" in field && !isBlank(field.default) ? castValue(field.default, field.cast) : field.default;
            op = "default";
          } else {
            value = null;
            op = field.from && field.from !== field.target ? "rename" : "copy";
          }
        } else {
          value = got.value;
          op = "copy";
          if (field.cast) {
            const casted = castValue(value, field.cast);
            if (casted !== value || typeof casted !== typeof value) op = "cast";
            value = casted;
          }
          if (op === "copy" && field.from && field.from !== field.target) op = "rename";
        }
      }
      if (field.required && isBlank(value)) {
        errors.push({field: field.target, message: "required field is missing"});
        failed.add(field.target);
        continue;
      }
      output[field.target] = value;
      env[field.target] = value;
      changes.push({field: field.target, op, before, after: value});
    } catch (err) {
      errors.push({field: field.target, message: err.message});
      failed.add(field.target);
    }
  }
  const used = consumedKeys(spec);
  const dropped = Object.keys(row).filter(key => !used.has(key));
  return {mapped: errors.length ? null : output, changes, errors, dropped};
}
function parseCsv(text) {
  const [header, ...lines] = text.split("\n");
  const columns = header.split(",");
  return {columns, rows: lines.filter(Boolean).map(line => Object.fromEntries(columns.map((column, i) => [column, line.split(",")[i]])))};
}

function boot(doc) {
  const $ = id => doc.getElementById(id);
  let store = new Map(), seen = new Set(), runs = 0;
  $("crm").textContent = JSON.stringify(CRM, null, 1);
  $("csv").textContent = CSV;
  $("yaml").textContent = `# CRM API\n${toYaml(CRM_SPEC)}\n\n# CSV export\n${toYaml(CSV_SPEC)}`;
  $("fail").oninput = event => { $("failv").textContent = event.target.value + "%"; };
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function log(msg, cls = "dim") {
    const span = doc.createElement("span");
    span.className = cls;
    span.textContent = msg + "\n";
    $("log").appendChild(span);
    $("log").scrollTop = 1e9;
  }
  async function mockGet(page) {
    const probability = +$("fail").value / 100;
    const roll = Math.random();
    if (roll < probability / 2) return {status: 429, headers: {"Retry-After": "1"}};
    if (roll < probability) return {status: [500, 502, 503][page % 3]};
    return {status: 200, body: CRM.slice(page * 2, page * 2 + 2), next: page * 2 + 2 < CRM.length ? page + 1 : null};
  }
  async function restGet(page, max = 5) {
    for (let attempt = 1; attempt <= max; attempt++) {
      const res = await mockGet(page);
      if (res.status === 200) {
        log(`  GET /contacts?page=${page} → 200 (${res.body.length} records)`, "ok");
        return res;
      }
      const wait = res.headers?.["Retry-After"] ? +res.headers["Retry-After"] * 1000 : Math.min(2000, 200 * 2 ** (attempt - 1));
      const why = res.status === 429 ? " (honoring Retry-After)" : " (exp. backoff)";
      log(`  GET /contacts?page=${page} → ${res.status}; retry ${attempt}/${max} in ${wait}ms${why}`, "warn");
      await sleep(wait / 4);
    }
    throw new Error("retries exhausted");
  }
  function fmt(value) {
    if (value == null) return "None";
    return typeof value === "string" ? `'${value}'` : String(value);
  }
  function logDryRun(title, rows, spec) {
    log(`tool dry_run('${title}')`, "");
    let ok = 0, bad = 0, changed = 0;
    const mapped = rows.map((row, index) => {
      const result = mapRecord(row, spec);
      const interesting = result.changes.filter(change => change.op !== "copy");
      if (result.errors.length) {
        bad++;
        log(`  record ${index}  ERROR`, "err");
        for (const change of interesting) {
          changed++;
          log(`    ${change.op} ${change.field}: ${fmt(change.before)} -> ${fmt(change.after)}`, "dim");
        }
        for (const err of result.errors) log(`    error ${err.field}: ${err.message}`, "err");
        return null;
      }
      ok++;
      changed += interesting.length;
      log(`  record ${index}  ok  ${interesting.map(change => `${change.op} ${change.field}`).join(", ")}`, "ok");
      if (result.dropped.length) log(`    dropped  ${result.dropped.join(", ")}`, "dim");
      return result.mapped;
    });
    log(`  ${ok} ok, ${bad} failed, ${changed} field change(s)`, "");
    return mapped;
  }
  function upsert(rec, key) {
    const id = rec.email;
    const existing = store.get(id);
    if (seen.has(key)) {
      log(`  ⤳ skip ${key}: idempotency key already applied`, "dim");
      return "skip";
    }
    seen.add(key);
    if (existing) {
      Object.assign(existing, Object.fromEntries(Object.entries(rec).filter(([, value]) => value)));
      existing.sources = [...new Set([...existing.sources, ...rec.sources])];
      log(`  ✎ merged ${id} (${existing.sources.join(" + ")})`, "ok");
      return "merge";
    }
    store.set(id, {...rec});
    log(`  + created ${id}`, "ok");
    return "create";
  }
  function esc(value) {
    return String(value).replace(/[&<>"']/g, ch => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }
  function render(stats) {
    $("stats").innerHTML = `<span class="ok">created ${stats.create}</span><span class="ok">merged ${stats.merge}</span><span class="dim">skipped (idempotent) ${stats.skip}</span><span class="err">rejected ${stats.reject}</span><span>total records ${store.size}</span>`;
    const head = "<tr><th>email</th><th>name</th><th>account</th><th>stage</th><th>phone</th><th>score</th><th>label</th><th>sources</th></tr>";
    const cells = rec => [rec.email, rec.name, rec.account, rec.stage, rec.phone || "—", rec.score ?? "—", rec.label, rec.sources.join(", ")]
      .map(value => `<td>${esc(value)}</td>`).join("");
    $("out").innerHTML = head + [...store.values()].map(rec => `<tr>${cells(rec)}</tr>`).join("");
  }
  async function run() {
    $("run").disabled = $("again").disabled = true;
    runs++;
    const started = Date.now();
    const stats = {create: 0, merge: 0, skip: 0, reject: 0};
    log(`\n▶ Run #${runs} · agent plan: fetch_crm → read_csv → dry_run mapping → upsert_records`, "");
    try {
      log("tool fetch_crm()", "");
      let page = 0;
      const crmRows = [];
      while (page !== null) {
        const response = await restGet(page);
        crmRows.push(...response.body);
        page = response.next;
      }
      const mappedCrm = logDryRun("crm_contacts.yaml", crmRows, CRM_SPEC);
      log("tool read_csv('acme_contacts.csv')", "");
      const table = parseCsv(CSV);
      const allowed = ["Contact ID", "Name", "E-mail", "Account", "Stage", "Phone", "Score"];
      if (table.columns.some(column => !allowed.includes(column))) throw new Error("unknown column");
      log(`  columns validated against allow-list (${table.columns.length})`, "ok");
      const mappedCsv = logDryRun("csv_contacts.yaml", table.rows, CSV_SPEC);
      log("tool upsert_records()", "");
      crmRows.forEach((row, index) => {
        const mapped = mappedCrm[index];
        if (!mapped) { stats.reject++; return; }
        const rec = {...mapped, sources: ["crm"]};
        const key = `crm:${row.id}:${row.updated}`;
        stats[upsert(rec, key)]++;
        if ($("dup").checked && row.id === "c_102") {
          log("  ↺ duplicate delivery of c_102 received", "warn");
          stats[upsert(rec, key)]++;
        }
      });
      table.rows.forEach((row, index) => {
        const mapped = mappedCsv[index];
        const key = `csv:${row["Contact ID"]}`;
        if (!mapped) {
          stats.reject++;
          log(`  ⤳ skip ${key}: validation failed`, "err");
          return;
        }
        stats[upsert({...mapped, sources: ["csv"]}, key)]++;
      });
      log(`✔ done in ${((Date.now() - started) / 1000).toFixed(1)}s`, "ok");
    } catch (err) {
      log("✖ " + err.message + " — sync halted safely, no partial duplicates", "err");
    }
    render(stats);
    $("run").disabled = $("again").disabled = false;
  }
  $("run").onclick = run;
  $("again").onclick = run;
  $("reset").onclick = () => {
    store.clear();
    seen.clear();
    runs = 0;
    $("log").textContent = "";
    $("out").innerHTML = "";
    $("stats").innerHTML = "";
  };
}

if (typeof document === "undefined") {
  globalThis.__demo = {mapRecord, toYaml, CRM_SPEC, CSV_SPEC, parseCsv, CSV, CRM};
} else {
  boot(document);
}

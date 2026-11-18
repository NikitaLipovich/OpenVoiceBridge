// The PC side of test_bench_ui.py (goal 104): the template engine and SPEC embedded in the generated benchd_admin.html,
// run in node with the same inputs as gen_bench_ui.py --render and the Java of the phone.
//   node bench_render.js PAGE.html panel STATUS.json page|phone
//   node bench_render.js PAGE.html cases CASES.json
"use strict";
const fs = require("fs");
const [page, mode, input, device] = process.argv.slice(2);
const html = fs.readFileSync(page, "utf8");
const engine = html.slice(html.indexOf("// @ENGINE-BEGIN"), html.indexOf("// @ENGINE-END"));
const T = new Function(engine + "\nreturn T;")();
const specLine = html.match(/^const SPEC = (.*);$/m);
const SPEC = JSON.parse(specLine[1].replace(/<\\\//g, "</"));
const data = JSON.parse(fs.readFileSync(input, "utf8"));
let out;
if (mode === "cases") {
  out = data.map((c) => ("tpl" in c ? T.render(c.tpl, c.ctx, c.vars || {}) : T.holds(c.conds, c.ctx, c.vars || {})));
} else {
  const vars = {device: device === "phone" ? "телефон" : "страница", owner: SPEC.owner};
  out = {header: T.render(SPEC.header, data, vars), fields: [], actions: []};
  for (const panel of SPEC.panels) {
    for (const f of panel.fields) {
      if (f.list) {
        const items = T.resolve(f.list, data, vars);
        (Array.isArray(items) ? items : []).forEach((it, i) => {
          const [text, color] = T.pick(f.cases, it, vars);
          out.fields.push({id: f.id + "." + i, label: T.render(f.label, it, vars), text, color});
        });
      } else {
        const [text, color] = T.pick(f.cases, data, vars);
        out.fields.push({id: f.id, label: T.render(f.label, data, vars), text, color});
      }
    }
  }
  for (const a of SPEC.actions) {
    const param = {};
    for (const p of a.params || []) param[p.name] = p.default === undefined ? "" : p.default;
    const pv = Object.assign({param}, vars);
    out.actions.push({id: a.id, visible: T.holds(a.visible_if, data, pv), available: T.holds(a.available_if, data, pv),
                      confirm: T.render(a.confirm, data, pv), confirm2: T.render(a.confirm2, data, pv),
                      body: T.renderBody(a.body, data, pv)});
  }
  out.lock_other = T.render(SPEC.texts.lock_other, data, vars);
}
process.stdout.write(T.jsonStr(out));

package com.openvoicebridge.scoprobe;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The PC side of test_bench_ui.py (goal 104): the phone's template code (BenchTemplate, MiniJson, generated BenchSpec)
 * on a desktop JVM, with the same inputs as gen_bench_ui.py --render and the page's JS.
 *
 *   java ... BenchRenderMain panel STATUS.json page|phone what the screen shows for this status, as JSON
 *   java ... BenchRenderMain cases CASES.json             [{tpl, ctx, vars} | {conds, ctx, vars}] -> [text | true/false]
 */
public final class BenchRenderMain implements BenchUi {
    private final List<BenchTemplate.Field> fields = new ArrayList<>();
    private final List<BenchTemplate.Action> actions = new ArrayList<>();
    private String headerTemplate;

    @Override
    public void header(String title, String template) {
        headerTemplate = template;
    }

    @Override
    public void panel(String id, String title) {
    }

    @Override
    public void field(BenchTemplate.Field f) {
        fields.add(f);
    }

    @Override
    public void actionRow() {
    }

    @Override
    public void action(BenchTemplate.Action a) {
        actions.add(a);
    }

    @Override
    public void results() {
    }

    @Override
    public void feed(BenchTemplate.Feed f) {
    }

    private static Map<String, Object> field(String id, String label, String[] tc) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("label", label);
        m.put("text", tc[0]);
        m.put("color", tc[1]);
        return m;
    }

    @SuppressWarnings("unchecked")
    public static void main(String[] args) throws Exception {
        Object input = MiniJson.parse(new String(Files.readAllBytes(Paths.get(args[1])), "UTF-8"));
        Object out;
        if ("cases".equals(args[0])) {
            List<Object> res = new ArrayList<>();
            for (Object c : (List<Object>) input) {
                Map<String, Object> m = (Map<String, Object>) c;
                Map<String, Object> vars = m.get("vars") instanceof Map ? (Map<String, Object>) m.get("vars")
                        : new HashMap<String, Object>();
                if (m.containsKey("tpl")) {
                    res.add(BenchTemplate.render((String) m.get("tpl"), m.get("ctx"), vars));
                } else {
                    List<Object> raw = (List<Object>) m.get("conds");
                    BenchTemplate.Cond[] conds = new BenchTemplate.Cond[raw.size()];
                    for (int k = 0; k < conds.length; k++) {
                        Map<String, Object> r = (Map<String, Object>) raw.get(k);
                        conds[k] = new BenchTemplate.Cond((String) r.get("path"), (String) r.get("op"), r.get("value"));
                    }
                    res.add(BenchTemplate.holds(conds, m.get("ctx"), vars));
                }
            }
            out = res;
        } else {
            BenchRenderMain ui = new BenchRenderMain();
            BenchSpec.build(ui);
            Map<String, Object> vars = new HashMap<>();
            // An ASCII key: a Cyrillic word on the Windows command line does not reach the JVM intact.
            vars.put("device", args.length > 2 && "phone".equals(args[2]) ? "телефон" : "страница");
            vars.put("owner", BenchSpec.OWNER);
            Map<String, Object> o = new LinkedHashMap<>();
            o.put("header", BenchTemplate.render(ui.headerTemplate, input, vars));
            List<Object> fl = new ArrayList<>();
            for (BenchTemplate.Field f : ui.fields) {
                if (f.list != null) {
                    Object items = BenchTemplate.resolve(f.list, input, vars);
                    if (items instanceof List) {
                        int i = 0;
                        for (Object it : (List<Object>) items) {
                            fl.add(field(f.id + "." + i, BenchTemplate.render(f.label, it, vars),
                                    BenchTemplate.pick(f.cases, it, vars)));
                            i++;
                        }
                    }
                } else {
                    fl.add(field(f.id, BenchTemplate.render(f.label, input, vars), BenchTemplate.pick(f.cases, input, vars)));
                }
            }
            o.put("fields", fl);
            List<Object> al = new ArrayList<>();
            for (BenchTemplate.Action a : ui.actions) {
                Map<String, Object> pv = new HashMap<>(vars);
                Map<String, Object> param = new LinkedHashMap<>();
                if (a.params != null) {
                    for (BenchTemplate.Param p : a.params) {
                        param.put(p.name, p.def);
                    }
                }
                pv.put("param", param);
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("id", a.id);
                m.put("visible", BenchTemplate.holds(a.visibleIf, input, pv));
                m.put("available", BenchTemplate.holds(a.availableIf, input, pv));
                m.put("confirm", BenchTemplate.render(a.confirm, input, pv));
                m.put("confirm2", BenchTemplate.render(a.confirm2, input, pv));
                m.put("body", BenchTemplate.renderBody(a.bodyJson, input, pv));
                al.add(m);
            }
            o.put("actions", al);
            o.put("lock_other", BenchTemplate.render(BenchSpec.text("lock_other"), input, vars));
            out = o;
        }
        System.out.write(BenchTemplate.jsonStr(out).getBytes("UTF-8"));
        System.out.flush();
    }
}

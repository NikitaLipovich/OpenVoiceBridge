package com.openvoicebridge.scoprobe;

import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The template language of the bench UI spec (goal 104) and the classes BenchSpec (generated) is made of.
 * The rules are those of gen_bench_ui.py (the reference) and of the JS in bench_ui_web.tmpl.html; a test on the PC
 * (tests/test_bench_ui.py) renders the same status samples with all three and compares. Plain Java: no Android here.
 *
 *   "{path}" or "{path|fmt}"; fmt: int mb1 mb0 hhmm hms opt_comma owners json; "$name" paths come from vars
 *   ($device, $owner, $param.x, $code, $json, ...); null or no value: "—".
 *   Conditions: path op value, op: eq ne lt le gt ge truthy falsy empty nonempty; a list of them is "and".
 */
final class BenchTemplate {
    static final String MISSING = "—";
    private static final Pattern PH = Pattern.compile("\\{([^{}|]+)(?:\\|([a-z_0-9]+))?\\}");
    private static final Pattern NUMERIC = Pattern.compile("^-?\\d+(\\.\\d+)?$");

    private BenchTemplate() {
    }

    // --- the classes of the spec -------------------------------------------------------------------------------------

    static final class Cond {
        final String path, op;
        final Object value;

        Cond(String path, String op, Object value) {
            this.path = path;
            this.op = op;
            this.value = value;
        }
    }

    static final class Case {
        final Cond[] conds;
        final String text, color;

        Case(Cond[] conds, String text, String color) {
            this.conds = conds;
            this.text = text;
            this.color = color;
        }
    }

    static final class Field {
        final String id, list, label;
        final Case[] cases;

        Field(String id, String list, String label, Case[] cases) {
            this.id = id;
            this.list = list;
            this.label = label;
            this.cases = cases;
        }
    }

    static final class Param {
        final String name, label, type, def, placeholder;
        final String[][] choices;

        Param(String name, String label, String type, String[][] choices, String def, String placeholder) {
            this.name = name;
            this.label = label;
            this.type = type;
            this.choices = choices;
            this.def = def;
            this.placeholder = placeholder;
        }
    }

    static final class Action {
        final String id, label, group, method, endpoint, bodyJson, confirm, confirm2, unavailableText;
        final int row;
        final Param[] params;
        final boolean danger, requiresLock;
        final Cond[] visibleIf, availableIf;
        final Case[] ok, fail;

        Action(String id, String label, String group, int row, String method, String endpoint, String bodyJson,
               Param[] params, String confirm, String confirm2, boolean danger, boolean requiresLock,
               Cond[] visibleIf, Cond[] availableIf, String unavailableText, Case[] ok, Case[] fail) {
            this.id = id;
            this.label = label;
            this.group = group;
            this.row = row;
            this.method = method;
            this.endpoint = endpoint;
            this.bodyJson = bodyJson;
            this.params = params;
            this.confirm = confirm;
            this.confirm2 = confirm2;
            this.danger = danger;
            this.requiresLock = requiresLock;
            this.visibleIf = visibleIf;
            this.availableIf = availableIf;
            this.unavailableText = unavailableText;
            this.ok = ok;
            this.fail = fail;
        }
    }

    static final class Feed {
        final String id, kind, title, method, endpoint, fixedJson, bad, cursor, limitParam;
        final int pollMs, firstLimit, limit, keep;
        final Param[] params;
        final String[] columns, hideKeys;

        Feed(String id, String kind, String title, String method, String endpoint, int pollMs, Param[] params,
             String fixedJson, String[] columns, String[] hideKeys, String bad, String cursor, String limitParam,
             int firstLimit, int limit, int keep) {
            this.id = id;
            this.kind = kind;
            this.title = title;
            this.method = method;
            this.endpoint = endpoint;
            this.pollMs = pollMs;
            this.params = params;
            this.fixedJson = fixedJson;
            this.columns = columns;
            this.hideKeys = hideKeys;
            this.bad = bad;
            this.cursor = cursor;
            this.limitParam = limitParam;
            this.firstLimit = firstLimit;
            this.limit = limit;
            this.keep = keep;
        }
    }

    // --- values --------------------------------------------------------------------------------------------------------

    static boolean isNum(Object v) {
        return v instanceof Long || v instanceof Double || v instanceof Integer;
    }

    static Double toNum(Object v) {
        if (isNum(v)) {
            return ((Number) v).doubleValue();
        }
        if (v instanceof String && NUMERIC.matcher((String) v).matches()) {
            return Double.parseDouble((String) v);
        }
        return null;
    }

    static String fixed(double x, int digits) {
        String s = new BigDecimal(x).setScale(digits, RoundingMode.HALF_UP).toPlainString();
        if (s.startsWith("-") && !s.matches(".*[1-9].*")) {
            s = s.substring(1);
        }
        return s;
    }

    static String numStr(double x) {
        if (x == Math.rint(x) && Math.abs(x) < 1e15) {
            return Long.toString((long) x);
        }
        String s = fixed(x, 3);
        int end = s.length();
        while (end > 0 && s.charAt(end - 1) == '0') {
            end--;
        }
        if (end > 0 && s.charAt(end - 1) == '.') {
            end--;
        }
        return s.substring(0, end);
    }

    static String quote(String s) {
        StringBuilder b = new StringBuilder("\"");
        for (int k = 0; k < s.length(); k++) {
            char c = s.charAt(k);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                case '\b': b.append("\\b"); break;
                case '\f': b.append("\\f"); break;
                default:
                    if (c < 0x20) {
                        b.append(String.format("\\u%04x", (int) c));
                    } else {
                        b.append(c);
                    }
            }
        }
        return b.append('"').toString();
    }

    static String jsonStr(Object v) {
        if (v == null) {
            return "null";
        }
        if (v instanceof Boolean) {
            return ((Boolean) v) ? "true" : "false";
        }
        if (isNum(v)) {
            return numStr(((Number) v).doubleValue());
        }
        if (v instanceof String) {
            return quote((String) v);
        }
        StringBuilder b = new StringBuilder();
        if (v instanceof List) {
            b.append('[');
            boolean first = true;
            for (Object x : (List<?>) v) {
                if (!first) {
                    b.append(',');
                }
                first = false;
                b.append(jsonStr(x));
            }
            return b.append(']').toString();
        }
        if (v instanceof Map) {
            b.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) {
                    b.append(',');
                }
                first = false;
                b.append(quote(String.valueOf(e.getKey()))).append(':').append(jsonStr(e.getValue()));
            }
            return b.append('}').toString();
        }
        return quote(String.valueOf(v));
    }

    static String valueStr(Object v) {
        if (v == null) {
            return MISSING;
        }
        if (v instanceof Boolean) {
            return ((Boolean) v) ? "да" : "нет";
        }
        if (isNum(v)) {
            return numStr(((Number) v).doubleValue());
        }
        if (v instanceof List || v instanceof Map) {
            return jsonStr(v);
        }
        return String.valueOf(v);
    }

    static boolean isEmpty(Object v) {
        return v == null || "".equals(v) || (v instanceof List && ((List<?>) v).isEmpty())
                || (v instanceof Map && ((Map<?, ?>) v).isEmpty());
    }

    static boolean truthy(Object v) {
        if (v == null || Boolean.FALSE.equals(v) || "".equals(v)) {
            return false;
        }
        if (isNum(v)) {
            return ((Number) v).doubleValue() != 0;
        }
        if (v instanceof List || v instanceof Map) {
            return !isEmpty(v);
        }
        return true;
    }

    static Object resolve(String path, Object ctx, Map<String, Object> vars) {
        String[] parts = path.trim().split("\\.");
        Object cur;
        int from = 0;
        if (parts[0].startsWith("$")) {
            cur = vars == null ? null : vars.get(parts[0].substring(1));
            from = 1;
        } else {
            cur = ctx;
        }
        for (int k = from; k < parts.length; k++) {
            String p = parts[k];
            if (cur instanceof Map) {
                cur = ((Map<?, ?>) cur).get(p);
            } else if (cur instanceof List && p.matches("\\d+") && Integer.parseInt(p) < ((List<?>) cur).size()) {
                cur = ((List<?>) cur).get(Integer.parseInt(p));
            } else {
                return null;
            }
        }
        return cur;
    }

    static String fmt(Object v, String name) {
        if (name == null) {
            return valueStr(v);
        }
        switch (name) {
            case "int": {
                Double n = toNum(v);
                return n == null ? MISSING : numStr(Math.floor(n + 0.5));
            }
            case "mb1":
            case "mb0": {
                Double n = toNum(v);
                return n == null ? MISSING : fixed(n / 1048576.0, "mb1".equals(name) ? 1 : 0);
            }
            case "hhmm":
            case "hms": {
                int end = "hhmm".equals(name) ? 16 : 19;
                return v instanceof String && ((String) v).length() >= end ? ((String) v).substring(11, end) : MISSING;
            }
            case "opt_comma":
                return isEmpty(v) || !truthy(v) ? "" : ", " + valueStr(v);
            case "owners": {
                if (!(v instanceof List)) {
                    return "";
                }
                StringBuilder b = new StringBuilder();
                for (Object x : (List<?>) v) {
                    if (x instanceof Map) {
                        if (b.length() > 0) {
                            b.append(", ");
                        }
                        b.append(valueStr(((Map<?, ?>) x).get("owner")));
                    }
                }
                return b.toString();
            }
            case "json":
                return jsonStr(v);
            default:
                throw new IllegalArgumentException("unknown format " + name);
        }
    }

    static String render(String tpl, Object ctx, Map<String, Object> vars) {
        if (tpl == null) {
            return "";
        }
        Matcher m = PH.matcher(tpl);
        StringBuffer out = new StringBuffer();
        while (m.find()) {
            m.appendReplacement(out, Matcher.quoteReplacement(fmt(resolve(m.group(1), ctx, vars), m.group(2))));
        }
        m.appendTail(out);
        return out.toString();
    }

    static boolean eq(Object a, Object b) {
        if (a instanceof Boolean || b instanceof Boolean) {
            return a != null && a.equals(b);
        }
        Double na = toNum(a), nb = toNum(b);
        if (na != null && nb != null) {
            return na.doubleValue() == nb.doubleValue();
        }
        if (a == null || b == null) {
            return a == null && b == null;
        }
        return a.getClass() == b.getClass() && a.equals(b);
    }

    static boolean holds(Cond[] conds, Object ctx, Map<String, Object> vars) {
        if (conds == null) {
            return true;
        }
        for (Cond c : conds) {
            Object v = resolve(c.path, ctx, vars);
            boolean ok;
            switch (c.op) {
                case "truthy": ok = truthy(v); break;
                case "falsy": ok = !truthy(v); break;
                case "empty": ok = isEmpty(v); break;
                case "nonempty": ok = !isEmpty(v); break;
                case "eq": ok = eq(v, c.value); break;
                case "ne": ok = !eq(v, c.value); break;
                default: {
                    Double x = toNum(v), y = toNum(c.value);
                    if (x == null || y == null) {
                        ok = false;
                    } else if ("lt".equals(c.op)) {
                        ok = x < y;
                    } else if ("le".equals(c.op)) {
                        ok = x <= y;
                    } else if ("gt".equals(c.op)) {
                        ok = x > y;
                    } else {
                        ok = x >= y;
                    }
                }
            }
            if (!ok) {
                return false;
            }
        }
        return true;
    }

    /** {text, color} of the first case whose conditions hold; {"", ""} when none does. */
    static String[] pick(Case[] cases, Object ctx, Map<String, Object> vars) {
        if (cases != null) {
            for (Case c : cases) {
                if (holds(c.conds, ctx, vars)) {
                    return new String[]{render(c.text, ctx, vars), c.color == null ? "" : c.color};
                }
            }
        }
        return new String[]{"", ""};
    }

    /** The fixed body of an action with its string values rendered. */
    @SuppressWarnings("unchecked")
    static Map<String, Object> renderBody(String bodyJson, Object ctx, Map<String, Object> vars) {
        Map<String, Object> body = (Map<String, Object>) MiniJson.parse(bodyJson);
        for (Map.Entry<String, Object> e : body.entrySet()) {
            if (e.getValue() instanceof String) {
                e.setValue(render((String) e.getValue(), ctx, vars));
            }
        }
        return body;
    }
}

package com.openvoicebridge.scoprobe;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * A small JSON reader for the bench screen (goal 104): objects keep their key order (LinkedHashMap), arrays are
 * ArrayList, numbers are Long (no fraction, no exponent) or Double, plus String, Boolean and null. Plain Java, so that
 * BenchTemplate is tested on the PC against the same samples as the web page and gen_bench_ui.py.
 */
final class MiniJson {
    private final String s;
    private int i;

    private MiniJson(String s) {
        this.s = s;
    }

    static Object parse(String text) {
        MiniJson p = new MiniJson(text);
        p.ws();
        Object v = p.value();
        p.ws();
        if (p.i != p.s.length()) {
            throw new IllegalArgumentException("JSON: text after the value at " + p.i);
        }
        return v;
    }

    private void ws() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }

    private IllegalArgumentException bad(String what) {
        return new IllegalArgumentException("JSON: " + what + " at " + i);
    }

    private Object value() {
        if (i >= s.length()) {
            throw bad("end of text");
        }
        char c = s.charAt(i);
        if (c == '{') {
            return object();
        }
        if (c == '[') {
            return array();
        }
        if (c == '"') {
            return string();
        }
        if (s.startsWith("true", i)) {
            i += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", i)) {
            i += 5;
            return Boolean.FALSE;
        }
        if (s.startsWith("null", i)) {
            i += 4;
            return null;
        }
        return number();
    }

    private Map<String, Object> object() {
        Map<String, Object> m = new LinkedHashMap<>();
        i++;
        ws();
        if (i < s.length() && s.charAt(i) == '}') {
            i++;
            return m;
        }
        while (true) {
            ws();
            if (i >= s.length() || s.charAt(i) != '"') {
                throw bad("a key");
            }
            String k = string();
            ws();
            if (i >= s.length() || s.charAt(i) != ':') {
                throw bad("':'");
            }
            i++;
            ws();
            m.put(k, value());
            ws();
            if (i < s.length() && s.charAt(i) == ',') {
                i++;
                continue;
            }
            if (i < s.length() && s.charAt(i) == '}') {
                i++;
                return m;
            }
            throw bad("',' or '}'");
        }
    }

    private List<Object> array() {
        List<Object> a = new ArrayList<>();
        i++;
        ws();
        if (i < s.length() && s.charAt(i) == ']') {
            i++;
            return a;
        }
        while (true) {
            ws();
            a.add(value());
            ws();
            if (i < s.length() && s.charAt(i) == ',') {
                i++;
                continue;
            }
            if (i < s.length() && s.charAt(i) == ']') {
                i++;
                return a;
            }
            throw bad("',' or ']'");
        }
    }

    private String string() {
        StringBuilder b = new StringBuilder();
        i++;
        while (i < s.length()) {
            char c = s.charAt(i++);
            if (c == '"') {
                return b.toString();
            }
            if (c != '\\') {
                b.append(c);
                continue;
            }
            if (i >= s.length()) {
                break;
            }
            char e = s.charAt(i++);
            switch (e) {
                case 'n': b.append('\n'); break;
                case 't': b.append('\t'); break;
                case 'r': b.append('\r'); break;
                case 'b': b.append('\b'); break;
                case 'f': b.append('\f'); break;
                case 'u':
                    if (i + 4 > s.length()) {
                        throw bad("\\u");
                    }
                    b.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                    i += 4;
                    break;
                default: b.append(e);
            }
        }
        throw bad("an unterminated string");
    }

    private Object number() {
        int start = i;
        boolean frac = false;
        while (i < s.length()) {
            char c = s.charAt(i);
            if (c == '.' || c == 'e' || c == 'E') {
                frac = true;
            } else if (!(c == '-' || c == '+' || (c >= '0' && c <= '9'))) {
                break;
            }
            i++;
        }
        String t = s.substring(start, i);
        if (t.isEmpty()) {
            throw bad("a value");
        }
        if (!frac) {
            try {
                return Long.parseLong(t);
            } catch (NumberFormatException e) {
                return Double.parseDouble(t);
            }
        }
        return Double.parseDouble(t);
    }
}

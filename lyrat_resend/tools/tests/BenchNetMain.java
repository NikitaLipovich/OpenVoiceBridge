package com.openvoicebridge.scoprobe;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The PC side of test_bench_find.py (goal 105): the phone's discovery (BenchFind) and direct mode (S3Direct) on a desktop
 * JVM, against the real benchd and s3_sim.py, and on the same inputs as their Python references.
 *
 *   verify CASES.json                     [{answer (JSON text), token, nonce}] -> [verdict]
 *   sign TOKEN NONCE ID PORT CONTROL NAME the signature;  id TOKEN  the instance id
 *   logic                                 backoff, subnet broadcast, typed addresses, choose
 *   find TOKEN PORTS TARGETS HINTS CACHE_ID CACHE_HOST [skip]   a whole search (lists comma separated, "-" for none)
 *   state FEED.json                       S3Direct.State fed like s3_direct.DirectState -> status
 *   ul HEX...                             records of log datagrams and the whole lines after the Joiner
 *   redact TEXT                           benchlib.redact
 *   s3 HOST TAP_PORT LOG_PORT TARGETS     find the S3 by UQ, then direct mode: status, a command, the log, LOG0, UX
 *   cmd TARGET HEX                        S3Direct.directCommand (the text as hex of UTF-8: argv is not)
 */
public final class BenchNetMain {
    private BenchNetMain() {
    }

    private static List<String> list(String csv) {
        List<String> out = new ArrayList<>();
        if (csv == null || csv.equals("-") || csv.isEmpty()) {
            return out;
        }
        out.addAll(Arrays.asList(csv.split(",")));
        return out;
    }

    private static String arg(String s) {
        return "-".equals(s) ? null : s;
    }

    private static void print(Object o) {
        System.out.write(BenchTemplate.jsonStr(o).getBytes(StandardCharsets.UTF_8), 0,
                BenchTemplate.jsonStr(o).getBytes(StandardCharsets.UTF_8).length);
        System.out.println();
        System.out.flush();
    }

    private static String read(String path) throws Exception {
        return new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8);
    }

    public static void main(String[] a) throws Exception {
        switch (a[0]) {
            case "verify": {
                List<Object> out = new ArrayList<>();
                for (Object c : (List<?>) MiniJson.parse(read(a[1]))) {
                    Map<?, ?> m = (Map<?, ?>) c;
                    BenchFind.Answer ans = BenchFind.parse((String) m.get("answer"), "10.0.0.1", 1, "broadcast");
                    out.add(BenchFind.verify(ans, (String) m.get("token"), (String) m.get("nonce")));
                }
                print(out);
                break;
            }
            case "sign":
                System.out.println(BenchFind.sign(a[1], a[2], a[3], Integer.parseInt(a[4]), "1".equals(a[5]), a[6]));
                break;
            case "id":
                System.out.println(BenchFind.discId(a[1]));
                break;
            case "logic": {
                Map<String, Object> out = new LinkedHashMap<>();
                List<Object> b = new ArrayList<>();
                for (int k = 0; k <= 7; k++) {
                    b.add(BenchFind.backoffMs(k));
                }
                out.put("backoff", b);
                List<Object> e = new ArrayList<>();
                for (int k = 0; k <= 3; k++) {
                    e.add(BenchFind.searchAfterErrors(k));
                }
                out.put("errors", e);
                out.put("subnet", Arrays.asList(BenchFind.subnetBroadcast("192.168.0.37", 24),
                        BenchFind.subnetBroadcast("10.1.2.3", 16), BenchFind.subnetBroadcast("172.16.5.9", 20),
                        BenchFind.subnetBroadcast("bad", 24), BenchFind.subnetBroadcast("1.2.3.4", 31)));
                out.put("host", Arrays.asList(BenchFind.hostPort("192.168.0.5"), BenchFind.hostPort("http://192.168.0.5:5980/"),
                        BenchFind.hostPort("192.168.0.5:x"), BenchFind.hostPort(""), BenchFind.hostPort(":5080")));
                List<BenchFind.Answer> ok = new ArrayList<>();
                String[][] rows = {{"10.0.0.2", "5080", "aaa", "30"}, {"10.0.0.3", "5080", "bbb", "10"},
                        {"10.0.0.4", "5980", "ccc", "20"}};
                for (String[] r : rows) {
                    BenchFind.Answer x = new BenchFind.Answer();
                    x.addr = r[0];
                    x.httpPort = Integer.parseInt(r[1]);
                    x.id = r[2];
                    x.ms = Long.parseLong(r[3]);
                    ok.add(x);
                }
                out.put("choose", Arrays.asList(BenchFind.choose(ok, "ccc", null).id, BenchFind.choose(ok, null, "10.0.0.2:5080").id,
                        BenchFind.choose(ok, "zzz", "10.9.9.9:1").id));
                out.put("acceptable", Arrays.asList(BenchFind.acceptable("ok", "t"), BenchFind.acceptable("unsigned", "t"),
                        BenchFind.acceptable("unsigned", ""), BenchFind.acceptable("bad_sig", ""),
                        BenchFind.acceptable("no_answer", "")));
                print(out);
                break;
            }
            case "find": {
                BenchFind.Config cfg = new BenchFind.Config();
                cfg.token = arg(a[1]) == null ? "" : a[1];
                for (String p : list(a[2])) {
                    cfg.ports.add(Integer.parseInt(p));
                }
                cfg.targets.addAll(list(a[3]));
                cfg.hints.addAll(list(a[4]));
                cfg.cacheId = arg(a[5]);
                cfg.cacheHost = arg(a[6]);
                cfg.skipHints = a.length > 7 && "skip".equals(a[7]);
                long t0 = System.nanoTime();
                BenchFind.Result r = BenchFind.find(BenchFind.SYSTEM, cfg, new SecureRandom());
                Map<String, Object> out = new LinkedHashMap<>();
                out.put("chosen", r.chosen == null ? null : r.chosen.toMap());
                List<Object> all = new ArrayList<>();
                for (BenchFind.Answer x : r.all) {
                    all.add(x.toMap());
                }
                out.put("all", all);
                out.put("line", r.line);
                out.put("ms", (System.nanoTime() - t0) / 1000000L);
                print(out);
                break;
            }
            case "state": {
                Map<?, ?> feed = (Map<?, ?>) MiniJson.parse(read(a[1]));
                S3Direct.State st = new S3Direct.State((String) feed.get("addr"), (Long) feed.get("lost_ms"));
                for (Object e : (List<?>) feed.get("events")) {
                    Map<?, ?> m = (Map<?, ?>) e;
                    if (m.containsKey("uq")) {
                        st.feedUq((Map<?, ?>) m.get("uq"), (Long) m.get("t"), (Long) m.get("ms"), (String) m.get("iso"));
                    } else {
                        st.feedLine((String) m.get("board"), (String) m.get("line"), (String) m.get("iso"));
                    }
                }
                st.datagrams = (Long) feed.get("datagrams");
                print(st.status((Long) feed.get("now")));
                break;
            }
            case "ul": {
                Map<String, Object> out = new LinkedHashMap<>();
                List<Object> recs = new ArrayList<>();
                List<Object> lines = new ArrayList<>();
                S3Direct.Joiner j = new S3Direct.Joiner();
                for (int k = 1; k < a.length; k++) {
                    byte[] d = hex(a[k]);
                    List<S3Direct.Rec> got = S3Direct.parseDatagram(d, d.length);
                    if (got == null) {
                        recs.add(null);
                        continue;
                    }
                    List<Object> one = new ArrayList<>();
                    for (S3Direct.Rec r : got) {
                        Map<String, Object> m = new LinkedHashMap<>();
                        m.put("board", (long) r.board);
                        m.put("level", (long) r.level);
                        m.put("boot", (long) r.boot);
                        m.put("seq", r.seq);
                        m.put("t_ms", r.tMs);
                        m.put("lost", r.lost);
                        m.put("text", r.text);
                        one.add(m);
                        for (S3Direct.Rec w : j.feed(r)) {
                            lines.add(S3Direct.boardName(w.board) + " " + w.text);
                        }
                    }
                    recs.add(one);
                }
                for (S3Direct.Rec w : j.flush()) {
                    lines.add(S3Direct.boardName(w.board) + " " + w.text);
                }
                out.put("records", recs);
                out.put("lines", lines);
                print(out);
                break;
            }
            case "redact":
                print(S3Direct.redact(a[1]));
                break;
            case "cmd": {
                Object res;
                try {
                    res = Arrays.asList((Object[]) S3Direct.directCommand(a[1],
                            new String(hex(a[2]), StandardCharsets.UTF_8)));
                } catch (IllegalArgumentException e) {
                    res = "refused: " + e.getMessage();
                }
                print(res);
                break;
            }
            case "s3":
                s3(a[1], Integer.parseInt(a[2]), Integer.parseInt(a[3]), list(a[4]));
                break;
            default:
                throw new IllegalArgumentException(a[0]);
        }
    }

    private static byte[] hex(String s) {
        byte[] b = new byte[s.length() / 2];
        for (int i = 0; i < b.length; i++) {
            b[i] = (byte) Integer.parseInt(s.substring(2 * i, 2 * i + 2), 16);
        }
        return b;
    }

    /** Direct mode against a simulator, the way BenchActivity drives it. */
    private static void s3(String host, int tap, int logPort, List<String> targets) throws Exception {
        Map<String, Object> out = new LinkedHashMap<>();
        List<S3Direct.Seen> seen = S3Direct.find(BenchFind.SYSTEM, targets, tap, 3, 900);
        List<Object> found = new ArrayList<>();
        for (S3Direct.Seen x : seen) {
            found.add(x.addr);
        }
        out.put("found", found);
        try (S3Direct.Link link = new S3Direct.Link(BenchFind.SYSTEM, host, tap, logPort)) {
            S3Direct.State st = new S3Direct.State(host, S3Direct.LOST_MS);
            long t0 = System.nanoTime();
            Map<?, ?> q = link.query();
            st.feedUq(q, (System.nanoTime() - t0) / 1000000L, 1L, "t");
            List<String> lines = new ArrayList<>();
            long end = System.nanoTime() + 4000000000L;
            long nextLease = 0;
            while (System.nanoTime() < end) {
                if (System.nanoTime() >= nextLease) {
                    link.logLease(3, 3);
                    nextLease = System.nanoTime() + S3Direct.LEASE_REPEAT_MS * 1000000L;
                }
                for (S3Direct.Rec r : link.readLog(200, st)) {
                    st.feedLine(S3Direct.boardName(r.board), r.text, "t");
                    lines.add(S3Direct.redact(S3Direct.formatLine(r, new Date())));
                }
            }
            out.put("lines", (long) lines.size());
            out.put("boards", Arrays.asList(countBoard(lines, " s3 "), countBoard(lines, " lyrat ")));
            out.put("sample", lines.isEmpty() ? null : lines.get(lines.size() - 1));
            out.put("log_reply", link.logReply);
            out.put("status", st.status((System.nanoTime() - t0) / 1000000L));
            S3Direct.Reply r = link.uc("s");
            out.put("uc_s", Arrays.asList((long) r.status, (long) r.attempts, r.text.length() > 0));
            S3Direct.Reply r2 = link.uc("nosuchcommand");
            out.put("uc_unknown", (long) r2.status);
            link.logStop();
            link.readLog(600, null);             // what was on its way
            List<S3Direct.Rec> after = link.readLog(2500, null);
            out.put("lines_after_log0", (long) after.size());
            Map<?, ?> before = link.query();
            S3Direct.Reply x = link.ux();
            out.put("ux", Arrays.asList((long) x.status, x.text));
            Thread.sleep(1500);
            Map<?, ?> later = null;
            for (int k = 0; k < 20 && (later == null || later.get("boot").equals(before.get("boot"))); k++) {
                later = link.query();
                Thread.sleep(300);
            }
            out.put("boot_changed", later != null && !later.get("boot").equals(before.get("boot")));
        }
        print(out);
    }

    private static long countBoard(List<String> lines, String tag) {
        long n = 0;
        for (String l : lines) {
            if (l.contains(tag)) {
                n++;
            }
        }
        return n;
    }
}

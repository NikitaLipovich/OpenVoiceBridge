package com.openvoicebridge.scoprobe;

import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Random;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The phone straight to the S3 when benchd is not found (goal 105, DECISIONS D5/D6), with the protocol of the PC's
 * tools (docs/S3_UART.md): UQ (answered by the S3 alone, never over the UART), UC <seq> <command> repeated with the same
 * seq (the LyraT runs a seq once), UX <boot>, and the debug log by LOG1/LOG0 (udplog.py: datagrams "UL", records of 18
 * bytes of header, long lines continued by "+ "). State builds the status in the shape of benchd's /status for the "s3"
 * and "lyrat" parts, so the panel of bench_ui_spec.json shows both modes; bench/s3_direct.py is the reference and
 * test_bench_find.py compares the two. Plain Java: the same test runs it on the PC against s3_sim.py.
 */
final class S3Direct {
    static final int TAP_PORT = 5006;
    static final int LOG_PORT = 5008;
    static final long LEASE_REPEAT_MS = 2000;
    static final long LOST_MS = 10000;
    static final String[] S3_DIRECT_CMDS = {"query", "restart"};
    private static final Pattern VER_LINE = Pattern.compile("\\bver=(\\S+)");
    private static final Pattern STAT_FIELDS = Pattern.compile("\\b(up|rssi|net|txp|selftest|ip)=(\\S+)");
    private static final Pattern MAC = Pattern.compile(
            "\\b([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\\b");
    private static final Pattern CALLER = Pattern.compile("(CLIP\\S*\\s*).*$");

    private S3Direct() {
    }

    static boolean isUq(String text) {
        return text.startsWith("{\"fw\":\"s3tap\",\"boot\"") && text.contains("\"tx_mode\"");
    }

    // --- finding the S3 ---------------------------------------------------------------------------------------------

    /** One S3 that answered UQ. */
    static final class Seen {
        String addr;
        long ms;
        Map<?, ?> uq;
    }

    /** UQ to every target (broadcast addresses and known S3 addresses) on the S3's request port, all that answer. */
    static List<Seen> find(BenchFind.Binder b, List<String> targets, int port, int sends, int windowMs) {
        List<Seen> out = new ArrayList<>();
        byte[] q = "UQ".getBytes(StandardCharsets.US_ASCII);
        long t0 = System.nanoTime();
        try (DatagramSocket s = new DatagramSocket()) {
            b.bind(s);
            s.setBroadcast(true);
            byte[] buf = new byte[1500];
            for (int k = 0; k < sends; k++) {
                for (String t : targets) {
                    try {
                        s.send(new DatagramPacket(q, q.length, InetAddress.getByName(t), port));
                    } catch (IOException ignored) {
                        // the others still count
                    }
                }
                long end = t0 + (long) (k + 1) * windowMs * 1000000L / sends;
                while (true) {
                    long left = (end - System.nanoTime()) / 1000000L;
                    if (left <= 0) {
                        break;
                    }
                    s.setSoTimeout((int) Math.max(1, left));
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    try {
                        s.receive(p);
                    } catch (SocketTimeoutException e) {
                        break;
                    }
                    String text = new String(p.getData(), 0, p.getLength(), StandardCharsets.ISO_8859_1);
                    String addr = p.getAddress().getHostAddress();
                    if (!isUq(text) || seen(out, addr)) {
                        continue;
                    }
                    Seen x = new Seen();
                    x.addr = addr;
                    x.ms = (System.nanoTime() - t0) / 1000000L;
                    try {
                        x.uq = (Map<?, ?>) MiniJson.parse(text);
                    } catch (RuntimeException e) {
                        continue;
                    }
                    out.add(x);
                }
            }
        } catch (IOException e) {
            // no socket on this network
        }
        return out;
    }

    private static boolean seen(List<Seen> list, String addr) {
        for (Seen x : list) {
            if (x.addr.equals(addr)) {
                return true;
            }
        }
        return false;
    }

    // --- the log ------------------------------------------------------------------------------------------------------

    /** One record of a log datagram (udplog.parse_records). */
    static final class Rec {
        int board, level, boot;
        long seq, tMs, lost;
        String text;
        long lastSeq;
    }

    static String boardName(int b) {
        switch (b) {
            case 0:
                return "s3";
            case 1:
                return "lyrat";
            case 2:
                return "u3";
            case 3:
                return "u1";
            case 4:
                return "u2";
            case 0x83:
                return "u1con";
            case 0x84:
                return "u2con";
            default:
                return "b" + b;
        }
    }

    private static long u32(byte[] d, int i) {
        return (d[i] & 0xffL) | (d[i + 1] & 0xffL) << 8 | (d[i + 2] & 0xffL) << 16 | (d[i + 3] & 0xffL) << 24;
    }

    private static int u16(byte[] d, int i) {
        return (d[i] & 0xff) | (d[i + 1] & 0xff) << 8;
    }

    /** The records of one datagram, or null when it is not a log datagram (udplog.parse_datagram). */
    static List<Rec> parseDatagram(byte[] d, int len) {
        if (len < 4 || d[0] != 'U' || d[1] != 'L' || d[2] != 1) {
            return null;
        }
        List<Rec> out = new ArrayList<>();
        int i = 4;
        while (i + 18 <= len) {
            Rec r = new Rec();
            r.board = d[i] & 0xff;
            r.level = d[i + 1] & 0xff;
            r.boot = u16(d, i + 2);
            r.seq = u32(d, i + 4);
            r.tMs = u32(d, i + 8);
            r.lost = u32(d, i + 12);
            int n = u16(d, i + 16);
            if (i + 18 + n > len) {
                break;
            }
            r.text = new String(d, i + 18, n, StandardCharsets.UTF_8);
            out.add(r);
            i += 18 + n;
        }
        if (out.size() == (d[3] & 0xff)) {
            return out;
        }
        return out.isEmpty() ? null : out;
    }

    /** Puts a long line back together from its records (udplog.Joiner). */
    static final class Joiner {
        private Rec held;

        List<Rec> feed(Rec r) {
            List<Rec> out = new ArrayList<>();
            Rec h = held;
            if (h != null && r.text.startsWith("+ ") && r.board == h.board && r.boot == h.boot && r.seq == h.lastSeq + 1) {
                h.text = h.text + " " + r.text.substring(2);
                h.lastSeq = r.seq;
                return out;
            }
            r.lastSeq = r.seq;
            held = r;
            if (h != null) {
                out.add(h);
            }
            return out;
        }

        List<Rec> flush() {
            List<Rec> out = new ArrayList<>();
            if (held != null) {
                out.add(held);
                held = null;
            }
            return out;
        }
    }

    /** A line as udplog.format_line writes it, with the phone's time. */
    static String formatLine(Rec r, Date pc) {
        String stamp = new SimpleDateFormat("HH:mm:ss.SSS", Locale.ROOT).format(pc);
        return String.format(Locale.ROOT, "%s %-5s %9.3f %s", stamp, boardName(r.board), r.tMs / 1000.0, r.text);
    }

    /** MAC prefixes and a caller's number hidden, as benchlib.redact (the file may leave the phone). */
    static String redact(String line) {
        if (line.contains("CLIP")) {
            Matcher m = CALLER.matcher(line);
            if (m.find()) {
                line = line.substring(0, m.start()) + m.group(1) + "<number>";
            }
        }
        Matcher m = MAC.matcher(line);
        StringBuffer sb = new StringBuffer();
        while (m.find()) {
            m.appendReplacement(sb, "xx:xx:xx:xx:" + m.group(5) + ":" + m.group(6));
        }
        m.appendTail(sb);
        return sb.toString();
    }

    // --- the status ---------------------------------------------------------------------------------------------------

    /** What the phone knows of the S3 and the LyraT in direct mode (s3_direct.DirectState). */
    static final class State {
        final String addr;
        final long lostMs;
        Map<?, ?> uq;
        Long uqT, uqMs;
        String seen;
        final Map<String, Object> stat = new LinkedHashMap<>();
        String verS3, verLyrat, lastS3, lastLyrat;
        long datagrams;

        State(String addr, long lostMs) {
            this.addr = addr;
            this.lostMs = lostMs;
        }

        void feedUq(Map<?, ?> q, long tMs, Long ms, String iso) {
            uq = q;
            uqT = tMs;
            uqMs = ms;
            seen = iso;
        }

        void feedLine(String board, String text, String iso) {
            boolean s3 = "s3".equals(board);
            if (!s3 && !"lyrat".equals(board)) {
                return;
            }
            Matcher m = VER_LINE.matcher(text);
            String ver = m.find() ? m.group(1) : null;
            if (s3) {
                lastS3 = iso;
                if (ver != null) {
                    verS3 = ver;
                }
                if (text.startsWith("stat up=")) {
                    stat.clear();
                    Matcher f = STAT_FIELDS.matcher(text);
                    while (f.find()) {
                        stat.put(f.group(1), f.group(2));
                    }
                }
            } else {
                lastLyrat = iso;
                if (ver != null) {
                    verLyrat = ver;
                }
            }
        }

        private Object uqv(String k) {
            return uq == null ? null : uq.get(k);
        }

        Map<String, Object> status(long nowMs) {
            boolean quiet = uqT == null || nowMs - uqT > lostMs;
            Object upMs = uqv("up_ms");
            Object lf = uqv("last_frame_ms");
            Object flags = uqv("lyrat_flags");
            Map<String, Object> s3 = new LinkedHashMap<>();
            s3.put("addr", addr);
            s3.put("found", addr != null && !addr.isEmpty());
            s3.put("lost", quiet);
            s3.put("boot", uqv("boot"));
            s3.put("up_s", upMs instanceof Long ? upS((Long) upMs) : null);
            s3.put("rssi", stat.get("rssi"));
            s3.put("net", stat.get("net"));
            s3.put("txp", stat.get("txp"));
            s3.put("selftest", stat.get("selftest"));
            s3.put("ver", verS3);
            s3.put("tx_on", uqv("tx_on"));
            s3.put("last_answer", seen);
            s3.put("answer_ms", uqMs);
            s3.put("last_line", lastS3);
            s3.put("datagrams", datagrams);
            Map<String, Object> ly = new LinkedHashMap<>();
            boolean lfBad = !(lf instanceof Long) || (Long) lf < 0 || (Long) lf > Math.max(3000, lostMs);
            ly.put("lost", quiet || lfBad);
            ly.put("up_s", uqv("lyrat_up_s"));
            ly.put("last_frame_ms", lf);
            ly.put("ver", verLyrat);
            ly.put("last_line", lastLyrat);
            ly.put("sco_open", flags instanceof Long && (Long) flags >= 0 ? ((Long) flags & 2) != 0 : null);
            ly.put("mic_muted", flags instanceof Long && (Long) flags >= 0 ? ((Long) flags & 4) != 0 : null);   // goal 111
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("mode", "s3-direct");
            out.put("s3", s3);
            out.put("lyrat", ly);
            return out;
        }
    }

    /** Seconds of uptime with one decimal, half up (the same in s3_direct.py). */
    static double upS(long upMs) {
        return Math.floor(upMs / 100.0 + 0.5) / 10.0;
    }

    /**
     * The line about the S3 as the phone sees it next to what benchd's /status says (D5): {text, color ok|warn|bad|""}.
     * seen: addresses that answered the phone's UQ; svcAddr/svcFound: s3.addr and s3.found of /status, null without it.
     */
    static String[] compare(List<String> seen, String svcAddr, Boolean svcFound, Long ms) {
        String phone = seen == null || seen.isEmpty() ? null : join(seen);
        String took = ms == null ? "" : ", ответ " + ms + " мс";
        if (svcFound == null) {
            return phone == null ? new String[]{"S3 (телефон): не найдена", "bad"}
                    : new String[]{"S3 (телефон): " + phone + took, "ok"};
        }
        boolean svc = svcFound && svcAddr != null && !svcAddr.isEmpty();
        if (phone == null) {
            return svc ? new String[]{"S3: телефон её не видит, служба видит " + svcAddr + " — проверьте Wi-Fi телефона", "warn"}
                    : new String[]{"S3 не найдена ни телефоном, ни службой", "bad"};
        }
        if (!svc) {
            return new String[]{"S3: служба её не видит, телефон видит " + phone + took, "warn"};
        }
        if (seen.contains(svcAddr)) {
            return new String[]{"S3 " + svcAddr + took + " — адрес совпадает со службой"
                    + (seen.size() > 1 ? " (ответили ещё: " + (seen.size() - 1) + ")" : ""), "ok"};
        }
        return new String[]{"S3: расхождение — служба видит " + svcAddr + ", телефон " + phone, "warn"};
    }

    private static String join(List<String> list) {
        StringBuilder sb = new StringBuilder();
        for (String s : list) {
            sb.append(sb.length() == 0 ? "" : ", ").append(s);
        }
        return sb.toString();
    }

    // --- commands -----------------------------------------------------------------------------------------------------

    /** What the console action does in direct mode: {"uc", text} for the LyraT, {"uq"|"ux", null} for the S3. */
    static String[] directCommand(String target, String text) {
        String t = text == null ? "" : text.trim();
        if (t.isEmpty()) {
            throw new IllegalArgumentException("нужна команда");
        }
        for (int i = 0; i < t.length(); i++) {
            if (t.charAt(i) < 0x20 || t.charAt(i) > 0x7e) {
                throw new IllegalArgumentException("команда консоли — только латиница, цифры и знаки");
            }
        }
        if ("lyrat".equals(target)) {
            return new String[]{"uc", t};
        }
        if ("s3".equals(target)) {
            if ("query".equals(t)) {
                return new String[]{"uq", null};
            }
            if ("restart".equals(t)) {
                return new String[]{"ux", null};
            }
        }
        throw new IllegalArgumentException("напрямую S3 понимает только: query, restart");
    }

    /** The answer of a command: status -1 when none came. */
    static final class Reply {
        int status = -1;
        String text = "";
        int attempts;
        long ms;
    }

    /** The control channel and the log of one S3. Not thread safe: one user at a time. */
    static final class Link implements AutoCloseable {
        final String addr;
        final int tapPort, logPort;
        private final DatagramSocket ctl, log;
        private final InetAddress to;
        private int seq;
        final Joiner joiner = new Joiner();
        String logReply;

        Link(BenchFind.Binder b, String addr, int tapPort, int logPort) throws IOException {
            this.addr = addr;
            this.tapPort = tapPort;
            this.logPort = logPort;
            this.to = InetAddress.getByName(addr);
            ctl = new DatagramSocket();
            log = new DatagramSocket();
            b.bind(ctl);
            b.bind(log);
            seq = 1 + new Random().nextInt(60000);
        }

        private void send(DatagramSocket s, String text, int port) throws IOException {
            byte[] q = text.getBytes(StandardCharsets.US_ASCII);
            s.send(new DatagramPacket(q, q.length, to, port));
        }

        /** The next datagram from this S3 on socket s within ms, or null. */
        private String recv(DatagramSocket s, long ms) throws IOException {
            long end = System.nanoTime() + ms * 1000000L;
            byte[] buf = new byte[2048];
            while (true) {
                long left = (end - System.nanoTime()) / 1000000L;
                if (left <= 0) {
                    return null;
                }
                s.setSoTimeout((int) Math.max(1, left));
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    s.receive(p);
                } catch (SocketTimeoutException e) {
                    return null;
                }
                if (p.getAddress().equals(to)) {
                    return new String(p.getData(), 0, p.getLength(), StandardCharsets.ISO_8859_1);
                }
            }
        }

        /** UQ: the S3's counters, or null after 4 tries of 0.5 s. */
        Map<?, ?> query() throws IOException {
            for (int k = 0; k < 4; k++) {
                send(ctl, "UQ", tapPort);
                long end = System.nanoTime() + 500000000L;
                String got;
                while ((got = recv(ctl, Math.max(1, (end - System.nanoTime()) / 1000000L))) != null) {
                    if (isUq(got)) {
                        Object o = MiniJson.parse(got);
                        return o instanceof Map ? (Map<?, ?>) o : null;
                    }
                }
            }
            return null;
        }

        /** One console command of the LyraT, repeated with the same seq (8 tries of 0.25 s) as s3_uart.command. */
        Reply uc(String text) throws IOException {
            seq = (seq + 1) & 0xFFFF;
            String prefix = "UR " + seq + " ";
            Reply r = new Reply();
            long t0 = System.nanoTime();
            for (int attempt = 1; attempt <= 8; attempt++) {
                r.attempts = attempt;
                send(ctl, "UC " + seq + " " + text, tapPort);
                long end = System.nanoTime() + 250000000L;
                String got;
                while ((got = recv(ctl, Math.max(1, (end - System.nanoTime()) / 1000000L))) != null) {
                    if (!got.startsWith(prefix)) {
                        continue;        // an old reply of a former attempt, or an answer to something else
                    }
                    int nl = got.indexOf('\n');
                    String head = nl < 0 ? got : got.substring(0, nl);
                    String[] parts = head.split(" ");
                    try {
                        r.status = Integer.parseInt(parts[2]);
                    } catch (RuntimeException e) {
                        r.status = 2;
                    }
                    r.text = nl < 0 ? "" : got.substring(nl + 1);
                    r.ms = (System.nanoTime() - t0) / 1000000L;
                    return r;
                }
            }
            r.ms = (System.nanoTime() - t0) / 1000000L;
            return r;
        }

        /** Restart of the S3: UX with its boot id from a fresh UQ; status 0 when the S3 took it. */
        Reply ux() throws IOException {
            Reply r = new Reply();
            long t0 = System.nanoTime();
            Map<?, ?> q = query();
            if (q == null || !(q.get("boot") instanceof Long)) {
                r.text = "S3 не ответила на UQ";
                return r;
            }
            String boot = String.valueOf(q.get("boot"));
            for (int k = 0; k < 4; k++) {
                r.attempts = k + 1;
                send(ctl, "UX " + boot, tapPort);
                String got = recv(ctl, 500);
                if (got != null && got.startsWith("UX ")) {
                    r.status = got.trim().endsWith(" 1") ? 0 : 1;
                    r.text = r.status == 0 ? "S3 перезапускается (boot " + boot + ")" : "S3 отказала: " + got.trim();
                    r.ms = (System.nanoTime() - t0) / 1000000L;
                    return r;
                }
            }
            r.ms = (System.nanoTime() - t0) / 1000000L;
            return r;
        }

        /** Takes (or keeps) the S3's log for 10 s: "LOG1 <s3 level> <lyrat level>". Repeat every LEASE_REPEAT_MS. */
        void logLease(int s3Level, int lyratLevel) throws IOException {
            send(log, "LOG1 " + s3Level + " " + lyratLevel, logPort);
        }

        /** Gives the log back at once: "LOG0" (benchd takes it with its next LOG1). */
        void logStop() {
            try {
                send(log, "LOG0", logPort);
            } catch (IOException ignored) {
                // the lease ends by itself after 10 s
            }
        }

        /** The whole lines that came within ms; the count of log datagrams goes to st when given. */
        List<Rec> readLog(long ms, State st) throws IOException {
            List<Rec> out = new ArrayList<>();
            long end = System.nanoTime() + ms * 1000000L;
            byte[] buf = new byte[2048];
            while (true) {
                long left = (end - System.nanoTime()) / 1000000L;
                if (left <= 0) {
                    break;
                }
                log.setSoTimeout((int) Math.max(1, left));
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    log.receive(p);
                } catch (SocketTimeoutException e) {
                    break;
                }
                if (!p.getAddress().equals(to)) {
                    continue;
                }
                if (p.getLength() >= 4 && buf[0] == 'L' && buf[1] == 'O' && buf[2] == 'G' && buf[3] == ' ') {
                    logReply = new String(buf, 0, p.getLength(), StandardCharsets.ISO_8859_1);
                    continue;
                }
                List<Rec> recs = parseDatagram(buf, p.getLength());
                if (recs == null) {
                    continue;
                }
                if (st != null) {
                    st.datagrams++;
                }
                for (Rec r : recs) {
                    out.addAll(joiner.feed(r));
                }
            }
            return out;
        }

        @Override
        public void close() {
            ctl.close();
            log.close();
        }
    }
}

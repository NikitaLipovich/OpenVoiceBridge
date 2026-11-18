package com.openvoicebridge.scoprobe;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * How the phone finds benchd on the PC (goal 105). Plain Java (java.net, javax.crypto): the PC test runs it on a desktop
 * JVM against the real benchd; BenchActivity runs it on the phone with a Binder that ties the sockets to the Wi-Fi.
 *
 * Order (DECISIONS D1): every hint (the cached address, then a typed or linked one) by GET /api/whoami?nonce=, then a
 * broadcast round "BENCHD? 2 <nonce>" to the discovery ports: 5 sends spread over 2.4 s to 255.255.255.255 and the
 * subnet's broadcast, answers for 3 s (on this Wi-Fi a broadcast is lost in 1 of 3-5 tries, and in bursts of 2-3 s:
 * 3 sends in 0.8 s all fell into one burst, goal 105 #004); it ends 0.4 s after the first answer that counts.
 * An answer counts only when it carries this round's
 * nonce and, when the phone holds the LAN token, a valid HMAC-SHA256 signature keyed by the token (D2): the token itself
 * is never sent anywhere before the service proved it holds it. benchd_client.verify_answer() is the reference.
 */
final class BenchFind {
    static final int PROTO = 2;
    /** When the sends of a round go out, ms from its start (spread over a burst of losses, #004). */
    static final int[] SEND_AT_MS = {0, 400, 900, 1600, 2400};
    static final int DISCOVERY_PORT = 5082;
    static final int HTTP_PORT = 5080;
    static final String OK = "ok", UNSIGNED = "unsigned", BAD_SIG = "bad_sig", WRONG_NONCE = "wrong_nonce",
            NOT_BENCHD = "not_benchd", NO_ANSWER = "no_answer", OLD = "old";

    private BenchFind() {
    }

    /** Ties sockets and HTTP to a network (the phone's Wi-Fi); the default leaves them to the system. */
    interface Binder {
        void bind(DatagramSocket s) throws IOException;

        HttpURLConnection open(URL url) throws IOException;
    }

    static final Binder SYSTEM = new Binder() {
        @Override
        public void bind(DatagramSocket s) {
        }

        @Override
        public HttpURLConnection open(URL url) throws IOException {
            return (HttpURLConnection) url.openConnection();
        }
    };

    /** One answer of a benchd, with what the phone made of it. */
    static final class Answer {
        String addr, id = "", name = "", version = "", nonce, sig, verdict, via;
        int httpPort, proto;
        boolean control;
        long ms;

        String hostPort() {
            return addr + ":" + httpPort;
        }

        Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("addr", addr);
            m.put("http_port", (long) httpPort);
            m.put("id", id);
            m.put("name", name);
            m.put("verdict", verdict);
            m.put("via", via);
            return m;
        }
    }

    /** What a search gave: the service chosen (or null), everything that answered, and a line for the screen. */
    static final class Result {
        Answer chosen;
        final List<Answer> all = new ArrayList<>();
        String line;

        /** Some benchd answered at all (even unconfirmed, old or foreign): the PC is on, so no direct mode (D6). */
        boolean pcPresent() {
            for (Answer a : all) {
                if (!NOT_BENCHD.equals(a.verdict) && !NO_ANSWER.equals(a.verdict)) {
                    return true;
                }
            }
            return false;
        }

        int count(String verdict) {
            int n = 0;
            for (Answer a : all) {
                if (verdict.equals(a.verdict)) {
                    n++;
                }
            }
            return n;
        }
    }

    /** What the search starts from. */
    static final class Config {
        String token = "";
        final List<String> hints = new ArrayList<>();          // "ip:port", the cache first
        String cacheId, cacheHost;
        final List<Integer> ports = new ArrayList<>();         // discovery ports
        final List<String> targets = new ArrayList<>();        // broadcast addresses
        int[] sendAtMs = SEND_AT_MS;
        int windowMs = 3000, probeMs = 1500, graceMs = 400;
        boolean skipHints;
    }

    // --- the protocol --------------------------------------------------------------------------------------------------

    static String newNonce(SecureRandom rnd) {
        byte[] b = new byte[16];
        rnd.nextBytes(b);
        return hex(b);
    }

    static String request(String nonce) {
        return "BENCHD? " + PROTO + " " + nonce;
    }

    static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder(b.length * 2);
        for (byte x : b) {
            sb.append(String.format(Locale.ROOT, "%02x", x & 0xff));
        }
        return sb.toString();
    }

    static String hmac(String key, String msg) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
            return hex(mac.doFinal(msg.getBytes(StandardCharsets.UTF_8)));
        } catch (java.security.GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }

    static String discId(String token) {
        return token == null || token.isEmpty() ? "" : hmac(token, "benchd-id").substring(0, 12);
    }

    static String message(String nonce, String id, int httpPort, boolean control, String name) {
        return "benchd-disc|" + PROTO + "|" + nonce + "|" + id + "|" + httpPort + "|" + (control ? 1 : 0) + "|" + name;
    }

    static String sign(String token, String nonce, String id, int httpPort, boolean control, String name) {
        return hmac(token, message(nonce, id, httpPort, control, name));
    }

    /** An answer from its JSON text; verdict NOT_BENCHD when it is none. */
    static Answer parse(String text, String addr, long ms, String via) {
        Answer a = new Answer();
        a.addr = addr;
        a.ms = ms;
        a.via = via;
        a.verdict = NOT_BENCHD;
        Object o;
        try {
            o = MiniJson.parse(text);
        } catch (RuntimeException e) {
            return a;
        }
        if (!(o instanceof Map)) {
            return a;
        }
        Map<?, ?> m = (Map<?, ?>) o;
        Object port = m.get("http_port");
        if (!Long.valueOf(1).equals(m.get("benchd")) || !(port instanceof Long)) {
            return a;
        }
        a.httpPort = ((Long) port).intValue();
        a.control = Boolean.TRUE.equals(m.get("control"));
        a.id = str(m.get("id"));
        a.name = str(m.get("name"));
        a.version = str(m.get("version"));
        a.nonce = m.get("nonce") instanceof String ? (String) m.get("nonce") : null;
        a.sig = m.get("sig") instanceof String ? (String) m.get("sig") : null;
        a.proto = m.get("proto") instanceof Long ? ((Long) m.get("proto")).intValue() : 0;
        a.verdict = null;
        return a;
    }

    private static String str(Object o) {
        return o == null ? "" : String.valueOf(o);
    }

    /** OK, UNSIGNED, OLD, BAD_SIG, WRONG_NONCE or NOT_BENCHD, as benchd_client.verify_answer. */
    static String verify(Answer a, String token, String nonce) {
        if (NOT_BENCHD.equals(a.verdict)) {
            return NOT_BENCHD;
        }
        if (a.proto == 0 && a.nonce == null) {
            return OLD;                      // the answer of goal 104 to the plain "BENCHD?"
        }
        if (a.nonce == null || !a.nonce.equals(nonce)) {
            return WRONG_NONCE;
        }
        if (a.sig == null || a.sig.isEmpty() || token == null || token.isEmpty()) {
            return UNSIGNED;
        }
        String want = sign(token, nonce, a.id, a.httpPort, a.control, a.name);
        return MessageDigest.isEqual(want.getBytes(StandardCharsets.US_ASCII), a.sig.getBytes(StandardCharsets.US_ASCII))
                ? OK : BAD_SIG;
    }

    /** With the token only a signed answer; without it an unsigned or old one too (read only: no token to give away). */
    static boolean acceptable(String verdict, String token) {
        return OK.equals(verdict)
                || ((UNSIGNED.equals(verdict) || OLD.equals(verdict)) && (token == null || token.isEmpty()));
    }

    /** Among the acceptable answers: the cached instance, else the cached address, else the fastest. */
    static Answer choose(List<Answer> ok, String cacheId, String cacheHost) {
        Answer best = null;
        for (Answer a : ok) {
            if (cacheId != null && !cacheId.isEmpty() && cacheId.equals(a.id)) {
                return a;
            }
        }
        for (Answer a : ok) {
            if (cacheHost != null && cacheHost.equals(a.hostPort())) {
                return a;
            }
        }
        for (Answer a : ok) {
            if (best == null || a.ms < best.ms) {
                best = a;
            }
        }
        return best;
    }

    // --- when -----------------------------------------------------------------------------------------------------------

    /** Pause before the next search after this many fruitless ones in a row: 3, 6, 12, 24, then 30 s. */
    static long backoffMs(int failures) {
        if (failures <= 0) {
            return 0;
        }
        long ms = 3000L << Math.min(failures - 1, 4);
        return Math.min(ms, 30000L);
    }

    /** A search after this many failed polls of /status in a row (a poll every 2 s). */
    static final int ERRORS_BEFORE_SEARCH = 2;

    static boolean searchAfterErrors(int consecutive) {
        return consecutive >= ERRORS_BEFORE_SEARCH;
    }

    /** The broadcast address of ip/prefix, or null. */
    static String subnetBroadcast(String ip, int prefix) {
        String[] p = ip == null ? new String[0] : ip.split("\\.");
        if (p.length != 4 || prefix < 1 || prefix > 30) {
            return null;
        }
        long v = 0;
        try {
            for (String x : p) {
                int n = Integer.parseInt(x);
                if (n < 0 || n > 255) {
                    return null;
                }
                v = (v << 8) | n;
            }
        } catch (NumberFormatException e) {
            return null;
        }
        long b = v | ((1L << (32 - prefix)) - 1);
        return ((b >> 24) & 255) + "." + ((b >> 16) & 255) + "." + ((b >> 8) & 255) + "." + (b & 255);
    }

    /** "ip:port" of a typed address ("http://" and a missing port allowed), or null. */
    static String hostPort(String typed) {
        if (typed == null) {
            return null;
        }
        String t = typed.trim();
        if (t.startsWith("http://")) {
            t = t.substring(7);
        }
        while (t.endsWith("/")) {
            t = t.substring(0, t.length() - 1);
        }
        if (t.isEmpty() || t.contains("/") || t.contains(" ")) {
            return null;
        }
        int colon = t.lastIndexOf(':');
        if (colon < 0) {
            return t + ":" + HTTP_PORT;
        }
        try {
            int port = Integer.parseInt(t.substring(colon + 1));
            if (port <= 0 || port > 65535 || colon == 0) {
                return null;
            }
        } catch (NumberFormatException e) {
            return null;
        }
        return t;
    }

    // --- the network ----------------------------------------------------------------------------------------------------

    /** GET /api/whoami at host:port with this nonce; verdict NO_ANSWER when nothing usable came. */
    static Answer probe(Binder b, String hostPort, String token, String nonce, int timeoutMs) {
        long t0 = System.nanoTime();
        HttpURLConnection c = null;
        String addr = hostPort.substring(0, hostPort.lastIndexOf(':'));
        try {
            c = b.open(new URL("http://" + hostPort + "/api/whoami?nonce=" + nonce));
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs);
            c.setUseCaches(false);
            int code = c.getResponseCode();
            if (code != 200) {
                Answer a = new Answer();
                a.addr = addr;
                a.via = "cache";
                String server = c.getHeaderField("Server");
                // 404 from a benchd of goal 104 (no whoami yet): a service without signature; anything else is none.
                a.verdict = code == 404 && server != null && server.startsWith("benchd/") ? OLD : NOT_BENCHD;
                a.httpPort = Integer.parseInt(hostPort.substring(hostPort.lastIndexOf(':') + 1));
                return a;
            }
            String text = new String(readAll(c.getInputStream()), StandardCharsets.UTF_8);
            Answer a = parse(text, addr, (System.nanoTime() - t0) / 1000000L, "cache");
            if (a.verdict == null) {
                a.verdict = verify(a, token, nonce);
            }
            return a;
        } catch (IOException | RuntimeException e) {
            Answer a = new Answer();
            a.addr = addr;
            a.via = "cache";
            a.verdict = NO_ANSWER;
            try {
                a.httpPort = Integer.parseInt(hostPort.substring(hostPort.lastIndexOf(':') + 1));
            } catch (NumberFormatException ignored) {
                // a bad hint: port 0
            }
            return a;
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    /** Every benchd that answers one broadcast round, verdicts filled in. The round ends at windowMs, or graceMs after
     * the first answer acceptable with this token. */
    static List<Answer> broadcast(Binder b, List<String> targets, List<Integer> ports, String token, String nonce,
                                  int[] sendAtMs, int windowMs, int graceMs) {
        List<Answer> out = new ArrayList<>();
        byte[] q = request(nonce).getBytes(StandardCharsets.US_ASCII);
        // The plain request of goal 104 too: an old benchd answers only that, and any answer means the PC is on.
        byte[] old = "BENCHD?".getBytes(StandardCharsets.US_ASCII);
        long t0 = System.nanoTime();
        long end = t0 + windowMs * 1000000L;
        int next = 0;
        try (DatagramSocket s = new DatagramSocket()) {
            b.bind(s);
            s.setBroadcast(true);
            byte[] buf = new byte[1024];
            while (true) {
                long now = System.nanoTime();
                if (now >= end) {
                    break;
                }
                if (next < sendAtMs.length && now >= t0 + sendAtMs[next] * 1000000L) {
                    next++;
                    for (String t : targets) {
                        InetAddress to;
                        try {
                            to = InetAddress.getByName(t);
                        } catch (IOException e) {
                            continue;
                        }
                        for (int p : ports) {
                            try {
                                s.send(new DatagramPacket(q, q.length, to, p));
                                s.send(new DatagramPacket(old, old.length, to, p));
                            } catch (IOException ignored) {
                                // one target may be unreachable; the others still count
                            }
                        }
                    }
                }
                long until = next < sendAtMs.length ? Math.min(end, t0 + sendAtMs[next] * 1000000L) : end;
                long left = (until - System.nanoTime()) / 1000000L;
                if (left <= 0) {
                    continue;
                }
                s.setSoTimeout((int) Math.max(1, left));
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    s.receive(p);
                } catch (SocketTimeoutException e) {
                    continue;
                }
                String text = new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8);
                Answer a = parse(text, p.getAddress().getHostAddress(), (System.nanoTime() - t0) / 1000000L, "broadcast");
                if (NOT_BENCHD.equals(a.verdict)) {
                    continue;
                }
                a.verdict = verify(a, token, nonce);
                int seen = -1;
                for (int i = 0; i < out.size(); i++) {
                    if (out.get(i).hostPort().equals(a.hostPort())) {
                        seen = i;
                        break;
                    }
                }
                if (seen < 0) {
                    out.add(a);
                } else if (OLD.equals(out.get(seen).verdict) && !OLD.equals(a.verdict)) {
                    a.ms = out.get(seen).ms;
                    out.set(seen, a);            // a new benchd answers both: its signed answer counts
                }
                if (acceptable(a.verdict, token)) {
                    end = Math.min(end, System.nanoTime() + graceMs * 1000000L);   // others may still answer
                }
            }
        } catch (IOException e) {
            // no socket on this network: nothing found
        }
        return out;
    }

    /** The whole search (D1): hints by HTTP, then a broadcast round; the chosen service and a line for the screen. */
    static Result find(Binder b, Config cfg, SecureRandom rnd) {
        Result r = new Result();
        if (!cfg.skipHints) {
            for (String h : cfg.hints) {
                if (h == null || h.isEmpty()) {
                    continue;
                }
                Answer a = probe(b, h, cfg.token, newNonce(rnd), cfg.probeMs);
                r.all.add(a);
                if (acceptable(a.verdict, cfg.token)) {
                    r.chosen = a;
                    r.line = describe(r, cfg);
                    return r;
                }
            }
        }
        List<Answer> got = broadcast(b, cfg.targets, cfg.ports, cfg.token, newNonce(rnd), cfg.sendAtMs, cfg.windowMs,
                cfg.graceMs);
        r.all.addAll(got);
        List<Answer> ok = new ArrayList<>();
        for (Answer a : got) {
            if (acceptable(a.verdict, cfg.token)) {
                ok.add(a);
            }
        }
        r.chosen = choose(ok, cfg.cacheId, cfg.cacheHost);
        r.line = describe(r, cfg);
        return r;
    }

    /** What the screen says about a search. */
    static String describe(Result r, Config cfg) {
        int bad = r.count(BAD_SIG), unsigned = r.count(UNSIGNED) + r.count(OLD);
        boolean token = cfg.token != null && !cfg.token.isEmpty();
        StringBuilder sb = new StringBuilder();
        if (r.chosen != null) {
            sb.append("найден ПК ").append(r.chosen.name.isEmpty() ? "" : r.chosen.name + " ").append(r.chosen.hostPort())
                    .append("cache".equals(r.chosen.via) ? " (адрес из кэша подтверждён)" : " (поиск в сети)");
            if (!OK.equals(r.chosen.verdict)) {
                sb.append(", без токена: только просмотр");
            }
            int others = 0;
            for (Answer a : r.all) {
                if (a != r.chosen && acceptable(a.verdict, cfg.token)) {
                    others++;
                }
            }
            if (others > 0) {
                sb.append("; ещё служб: ").append(others);
            }
            if (bad + (token ? unsigned : 0) > 0) {
                sb.append("; чужих не взято: ").append(bad + (token ? unsigned : 0));
            }
            return sb.toString();
        }
        if (bad > 0) {
            return "ПК не найден: служба benchd отвечает, но подпись не сходится с токеном — чужой ПК или токен устарел "
                    + "(bench.py phone-link на своём ПК)";
        }
        if (token && unsigned > 0) {
            return "ПК не найден: служба benchd отвечает без подписи — старая версия, перезапустите benchd на ПК";
        }
        return "ПК не найден: benchd запущен с --lan? телефон в той же Wi-Fi? брандмауэр Windows пускает python "
                + "(UDP " + (cfg.ports.isEmpty() ? DISCOVERY_PORT : cfg.ports.get(0)) + ", TCP)? нет ли изоляции "
                + "клиентов в настройках Wi-Fi?";
    }

    static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }
}

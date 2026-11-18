package com.openvoicebridge.scoprobe;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.text.SpannableStringBuilder;
import android.text.Spanned;
import android.text.style.ForegroundColorSpan;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;

import com.openvoicebridge.scoprobe.BenchTemplate.Action;
import com.openvoicebridge.scoprobe.BenchTemplate.Feed;
import com.openvoicebridge.scoprobe.BenchTemplate.Field;
import com.openvoicebridge.scoprobe.BenchTemplate.Param;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.net.ConnectException;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.URLEncoder;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.regex.Pattern;

/**
 * The "Стенд" screen (goals 104, 105): the bench service benchd on the PC, controlled from the phone over the Wi-Fi,
 * and the S3 directly when the PC is off. What is on it comes from the generated BenchSpec (bench_ui_spec.json, the same
 * spec as benchd's web page); this class is the hand-written runtime.
 *
 * Finding the PC (goal 105, DECISIONS D1-D4): no address is given; BenchFind looks on every start of the screen, after
 * two failed polls, on a change of the Wi-Fi and by the button: the cached address first (GET /api/whoami with a nonce),
 * then a broadcast round. Only a service that signs the nonce with the LAN token gets the token; the cache is a hint.
 * The token comes once from `bench.py phone-link` as files/bench_link.json in the app's external files dir (only adb and
 * this app can write it), read once and deleted, kept in the private preferences. Sockets and HTTP go over the Wi-Fi
 * network even when Android's default network is the mobile one.
 *
 * Modes (D6): SERVICE - everything through benchd, the phone sends the S3 nothing but UQ (read only, to compare the
 * address); DIRECT - no benchd answered at all and the S3 did: status by UQ, the log by LOG1 into a file of the app,
 * the actions with "s3-direct" in the spec by UC/UX, each after a fresh search for benchd (never two masters); NONE.
 * Every write to the S3 is in DirectRun and sendDirect only.
 */
public class BenchActivity extends Activity implements BenchUi {
    static final String LINK_FILE = "bench_link.json";
    static final String LOG_DIR = "direct_logs";
    private static final long LOG_DIR_MAX = 20L * 1024 * 1024;
    private static final int COLOR_OK = Color.rgb(0x18, 0x79, 0x4e);
    private static final int COLOR_WARN = Color.rgb(0xa1, 0x62, 0x07);
    private static final int COLOR_BAD = Color.rgb(0xc2, 0x41, 0x0c);
    private static final int COLOR_MUTE = Color.rgb(0x66, 0x70, 0x7c);
    private static final long S3_LOOK_MS = 15000;           // UQ broadcast while the service is there (read only)
    private static final long DIRECT_SEARCH_MS = 5000;      // a search for benchd this often in direct mode
    private static final int DIRECT_AFTER_FAILURES = 2;     // fruitless searches in a row before direct mode (#004)
    private static final int MODE_NONE = 0, MODE_SERVICE = 1, MODE_DIRECT = 2;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService net = Executors.newFixedThreadPool(3);
    private final SecureRandom rnd = new SecureRandom();
    private SharedPreferences prefs;
    private LinearLayout root;
    private LinearLayout current;          // the panel being built
    private LinearLayout currentRow;
    private EditText hostEdit, tokenEdit;
    private TextView modeView, connView, s3View, svcView, updatedView, msgView, jobView, barView;
    private final Map<String, Object> vars = new HashMap<>();
    private final List<Field> fields = new ArrayList<>();
    private final Map<String, TextView> fieldViews = new HashMap<>();
    private final Map<String, View> fieldRows = new HashMap<>();
    private final Map<String, LinearLayout> listViews = new HashMap<>();
    private final Map<View, Integer> panelVia = new LinkedHashMap<>();
    private final List<Action> actions = new ArrayList<>();
    private final Map<String, View> actionViews = new HashMap<>();
    private final Map<String, Button> actionButtons = new HashMap<>();
    private final Map<String, Boolean> routeOk = new HashMap<>();
    private final Map<String, View> paramViews = new HashMap<>();
    private final List<Feed> feeds = new ArrayList<>();
    private final Map<String, TextView> feedViews = new HashMap<>();
    private final Map<String, Long> feedAfter = new HashMap<>();
    private final Map<String, List<String>> feedLines = new HashMap<>();
    private final Map<String, CheckBox> feedPause = new HashMap<>();
    private String headerTemplate = "";
    private Object status = null;
    private String lastJob = null;
    private boolean running = false;

    // finding (main thread only, except the flag)
    private final AtomicBoolean searching = new AtomicBoolean(false);
    private int mode = MODE_NONE;
    private String svcHost;                 // "ip:port" of the service in use
    private boolean svcVerified;            // it signed our nonce: the token may go there
    private int failures, pollErrors;
    private long nextSearchAt, nextS3At;
    private volatile Network wifi;
    private volatile String wifiIp;
    private volatile int wifiPrefix;
    private volatile String netKey;          // "network/ip" of the Wi-Fi now
    private String svcNetKey;                // ... when the service in use was found
    private ConnectivityManager cm;
    private ConnectivityManager.NetworkCallback netCallback;
    private List<String> s3Seen = new ArrayList<>();
    private Long s3Ms;
    private DirectRun direct;

    // --- life ------------------------------------------------------------------------------------------------------

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("bench", MODE_PRIVATE);
        migrate();
        vars.put("device", "телефон");
        vars.put("owner", BenchSpec.OWNER);
        // A fixed line on top for the answer of the last action (the buttons may be far down the page), then the page.
        LinearLayout frame = new LinearLayout(this);
        frame.setOrientation(LinearLayout.VERTICAL);
        frame.setPadding(0, 64, 0, 0);
        barView = text("", 14, true);
        barView.setPadding(24, 8, 24, 8);
        barView.setTextColor(Color.rgb(0x25, 0x63, 0xeb));
        barView.setContentDescription("msg-bar");
        frame.addView(barView);
        ScrollView scroll = new ScrollView(this);
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(24, 0, 24, 48);
        scroll.addView(root);
        frame.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(frame);
        cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        buildConnection();
        BenchSpec.build(this);
        applyMode();
    }

    @Override
    protected void onResume() {
        super.onResume();
        importLink();
        running = true;
        watchNetwork();
        failures = 0;
        search("экран открыт", false);
        tick.run();
        for (final Feed f : feeds) {
            schedule(f);
        }
    }

    @Override
    protected void onPause() {
        running = false;
        main.removeCallbacksAndMessages(null);
        stopDirect(null);          // LOG0: the log goes back to whoever asks next; no command leaves without the screen
        if (netCallback != null) {
            try {
                cm.unregisterNetworkCallback(netCallback);
            } catch (RuntimeException ignored) {
                // not registered
            }
            netCallback = null;
        }
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        net.shutdownNow();
        super.onDestroy();
    }

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            long now = System.currentTimeMillis();
            if (mode == MODE_SERVICE) {
                refreshStatus();
                refreshJob();
                if (now >= nextS3At) {
                    nextS3At = now + S3_LOOK_MS;
                    lookS3();
                }
            } else if (now >= nextSearchAt) {
                search(mode == MODE_DIRECT ? "проверка, не включился ли ПК" : "повтор", false);
            }
            main.postDelayed(this, BenchSpec.POLL_STATUS_MS);
        }
    };

    private void schedule(final Feed f) {
        main.postDelayed(new Runnable() {
            @Override
            public void run() {
                if (!running) {
                    return;
                }
                if (mode == MODE_SERVICE && (f.via & BenchTemplate.VIA_BENCHD) != 0) {
                    refreshFeed(f);
                }
                main.postDelayed(this, f.pollMs);
            }
        }, 300);
    }

    // --- the Wi-Fi network ------------------------------------------------------------------------------------------

    /** Sockets and HTTP on the Wi-Fi (not on the mobile network that Android may prefer when the Wi-Fi has no internet). */
    private BenchFind.Binder binder() {
        final Network n = wifi;
        if (n == null) {
            return BenchFind.SYSTEM;
        }
        return new BenchFind.Binder() {
            @Override
            public void bind(DatagramSocket s) throws IOException {
                n.bindSocket(s);
            }

            @Override
            public HttpURLConnection open(URL url) throws IOException {
                return (HttpURLConnection) n.openConnection(url);
            }
        };
    }

    private final Runnable networkChanged = new Runnable() {
        @Override
        public void run() {
            if (!running) {
                return;
            }
            if (wifi != null && mode == MODE_SERVICE && (svcNetKey == null || svcNetKey.equals(netKey))) {
                // The network as it was when the service was found (or not known yet then: the first event after
                // registering the callback): no change, no search.
                svcNetKey = netKey;
                return;
            }
            failures = 0;
            if (wifi == null) {
                stopDirect("нет Wi-Fi: телефон не в сети ПК");
                setMode(MODE_NONE);
                show(connView, "нет Wi-Fi: телефон не в сети ПК и S3", COLOR_BAD);
                return;
            }
            search("сменилась сеть", false);
        }
    };

    private void watchNetwork() {
        if (netCallback != null) {
            return;
        }
        netCallback = new ConnectivityManager.NetworkCallback() {
            private String key = "";

            @Override
            public void onLinkPropertiesChanged(Network n, LinkProperties lp) {
                String ip = null;
                int prefix = 0;
                for (LinkAddress la : lp.getLinkAddresses()) {
                    if (la.getAddress() instanceof Inet4Address) {
                        ip = la.getAddress().getHostAddress();
                        prefix = la.getPrefixLength();
                    }
                }
                boolean changed = !(n + "/" + ip).equals(key);
                wifi = n;
                wifiIp = ip;
                wifiPrefix = prefix;
                key = n + "/" + ip;
                netKey = key;
                if (changed) {
                    main.removeCallbacks(networkChanged);
                    main.postDelayed(networkChanged, 1000);        // one search for a burst of events
                }
            }

            @Override
            public void onLost(Network n) {
                if (n.equals(wifi)) {
                    wifi = null;
                    wifiIp = null;
                    key = "";
                    netKey = null;
                    main.removeCallbacks(networkChanged);
                    main.postDelayed(networkChanged, 1000);
                }
            }
        };
        try {
            cm.registerNetworkCallback(new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .build(), netCallback);
        } catch (RuntimeException e) {
            netCallback = null;
            say("сеть не отслеживается: " + e.getMessage());
        }
    }

    private List<String> broadcastTargets() {
        List<String> t = new ArrayList<>();
        t.add("255.255.255.255");
        String sub = BenchFind.subnetBroadcast(wifiIp, wifiPrefix);
        if (sub != null) {
            t.add(sub);
        }
        return t;
    }

    // --- connection: token, hints, the cache ----------------------------------------------------------------------

    private void buildConnection() {
        TextView title = text(BenchSpec.TITLE, 20, true);
        root.addView(title);
        modeView = text("ищу ПК…", 14, true);
        modeView.setContentDescription("mode");
        root.addView(modeView);
        connView = text("", 13, false);
        connView.setContentDescription("conn");
        root.addView(connView);
        s3View = text("", 13, false);
        s3View.setContentDescription("s3line");
        root.addView(s3View);
        LinearLayout row = hbox();
        Button find = button("Найти ПК", false);
        find.setContentDescription("find");
        find.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                failures = 0;
                search("кнопка", true);
            }
        });
        row.addView(find);
        Button refresh = button("Обновить", false);
        refresh.setContentDescription("refresh");
        refresh.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                refreshNow();
            }
        });
        row.addView(refresh);
        root.addView(row);
        // The way back when nothing is found: an address typed by hand (checked the same way) and the token.
        row = hbox();
        hostEdit = new EditText(this);
        hostEdit.setHint("адрес ПК вручную, если не находится: IP:5080");
        hostEdit.setSingleLine(true);
        hostEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostEdit.setText(prefs.getString("hint", ""));
        hostEdit.setContentDescription("host");
        row.addView(hostEdit, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(row);
        row = hbox();
        tokenEdit = new EditText(this);
        tokenEdit.setHint("токен управления (bench.py phone-link)");
        tokenEdit.setSingleLine(true);
        tokenEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        tokenEdit.setText(prefs.getString("token", ""));
        tokenEdit.setContentDescription("token");
        row.addView(tokenEdit, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        Button save = button("Сохранить", false);
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                String typed = hostEdit.getText().toString().trim();
                if (!typed.isEmpty() && BenchFind.hostPort(typed) == null) {
                    say("адрес не понят: нужно IP или IP:порт");
                    return;
                }
                prefs.edit().putString("hint", typed).putString("token", tokenEdit.getText().toString().trim()).apply();
                say("сохранено, проверяю");
                failures = 0;
                search("введён адрес", false);
            }
        });
        row.addView(save);
        root.addView(row);
    }

    /** The address of goal 104 ("host") becomes a hint: it is checked like the cache, never trusted. */
    private void migrate() {
        String old = prefs.getString("host", null);
        if (old != null) {
            prefs.edit().remove("host").putString("hint", old).apply();
        }
    }

    /** files/bench_link.json from `bench.py phone-link`: read once, kept in the preferences, the file deleted. */
    private void importLink() {
        File dir = getExternalFilesDir(null);
        File f = dir == null ? null : new File(dir, LINK_FILE);
        if (f == null || !f.isFile()) {
            return;
        }
        try (InputStream in = new FileInputStream(f)) {
            Object o = MiniJson.parse(new String(readAll(in), StandardCharsets.UTF_8));
            if (!(o instanceof Map)) {
                throw new IllegalArgumentException("not an object");
            }
            Map<?, ?> m = (Map<?, ?>) o;
            SharedPreferences.Editor e = prefs.edit();
            if (m.get("token") instanceof String) {
                e.putString("token", (String) m.get("token"));
                tokenEdit.setText((String) m.get("token"));
            }
            Object hint = m.get("hint") != null ? m.get("hint") : m.get("host");      // "host": a link file of 104
            e.putString("hint", hint instanceof String ? (String) hint : "");
            hostEdit.setText(hint instanceof String ? (String) hint : "");
            if (Boolean.TRUE.equals(m.get("forget"))) {
                e.remove("cache").remove("s3_cache");
            }
            for (String k : new String[]{"discovery_port", "s3_port", "log_port"}) {
                if (m.get(k) instanceof Long) {
                    e.putInt(k, ((Long) m.get(k)).intValue());
                } else {
                    e.remove(k);
                }
            }
            e.apply();
            say("связь с ПК получена: " + (m.get("token") instanceof String ? "токен" : "без токена")
                    + (Boolean.TRUE.equals(m.get("forget")) ? ", кэш адреса забыт" : ""));
        } catch (IOException | RuntimeException e) {
            say("файл связи с ПК не прочитан: " + e.getMessage());
        } finally {
            if (!f.delete()) {
                say("файл связи с ПК не удалён");
            }
        }
    }

    private String token() {
        return prefs.getString("token", "");
    }

    private Map<?, ?> cache() {
        try {
            Object o = MiniJson.parse(prefs.getString("cache", "null"));
            return o instanceof Map ? (Map<?, ?>) o : null;
        } catch (RuntimeException e) {
            return null;
        }
    }

    private int port(String key, int def) {
        int p = prefs.getInt(key, 0);
        return p > 0 ? p : def;
    }

    // --- the search ------------------------------------------------------------------------------------------------

    /** One search for benchd (and the S3) in a worker; the result goes to found(). Never two at once. */
    private void search(final String why, final boolean skipHints) {
        if (!searching.compareAndSet(false, true)) {
            return;
        }
        if (mode == MODE_NONE) {           // in direct mode the line keeps saying so: searches run every 5 s there
            show(modeView, "ищу ПК… (" + why + ")", COLOR_MUTE);
        }
        final BenchFind.Config cfg = config(skipHints);
        final BenchFind.Binder b = binder();
        final List<String> s3Targets = s3Targets();
        final int s3Port = port("s3_port", S3Direct.TAP_PORT);
        net.execute(new Runnable() {
            @Override
            public void run() {
                BenchFind.Result r;
                List<S3Direct.Seen> s3 = null;
                try {
                    r = BenchFind.find(b, cfg, rnd);
                    if (r.chosen == null && direct == null) {
                        s3 = S3Direct.find(b, s3Targets, s3Port, 3, 900);
                    }
                } finally {
                    searching.set(false);
                }
                final BenchFind.Result fr = r;
                final List<S3Direct.Seen> fs3 = s3;
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        found(fr, fs3, cfg);
                    }
                });
            }
        });
    }

    private BenchFind.Config config(boolean skipHints) {
        BenchFind.Config cfg = new BenchFind.Config();
        cfg.token = token();
        Map<?, ?> c = cache();
        if (c != null && c.get("host") instanceof String && c.get("port") instanceof Long) {
            cfg.cacheHost = c.get("host") + ":" + c.get("port");
            cfg.cacheId = c.get("id") instanceof String ? (String) c.get("id") : null;
            cfg.hints.add(cfg.cacheHost);
        }
        String hint = BenchFind.hostPort(prefs.getString("hint", ""));
        if (hint != null && !cfg.hints.contains(hint)) {
            cfg.hints.add(hint);
        }
        cfg.ports.add(port("discovery_port", BenchFind.DISCOVERY_PORT));
        cfg.targets.addAll(broadcastTargets());
        cfg.skipHints = skipHints;
        return cfg;
    }

    private List<String> s3Targets() {
        List<String> t = broadcastTargets();
        String cached = prefs.getString("s3_cache", "");
        if (!cached.isEmpty()) {
            t.add(cached);
        }
        Object svc = BenchTemplate.resolve("s3.addr", status, null);
        if (mode == MODE_SERVICE && svc instanceof String && !t.contains(svc)) {
            t.add((String) svc);
        }
        return t;
    }

    private void found(BenchFind.Result r, List<S3Direct.Seen> s3, BenchFind.Config cfg) {
        if (!running) {
            return;
        }
        if (s3 != null) {
            noteS3(s3);
        }
        if (r.chosen != null) {
            boolean wasDirect = direct != null;
            stopDirect(null);
            BenchFind.Answer a = r.chosen;
            svcHost = a.hostPort();
            svcVerified = BenchFind.OK.equals(a.verdict);
            svcNetKey = netKey;
            Map<String, Object> c = new LinkedHashMap<>();
            c.put("host", a.addr);
            c.put("port", (long) a.httpPort);
            c.put("id", a.id);
            c.put("name", a.name);
            c.put("t_ms", System.currentTimeMillis());
            c.put("via", a.via);
            prefs.edit().putString("cache", BenchTemplate.jsonStr(c)).apply();
            failures = 0;
            pollErrors = 0;
            setMode(MODE_SERVICE);
            show(connView, r.line, svcVerified ? COLOR_OK : COLOR_WARN);
            if (wasDirect) {
                say(BenchSpec.text("direct_pc_back"));
            }
            nextS3At = 0;
            refreshNow();
            return;
        }
        failures++;
        if (mode == MODE_SERVICE) {
            // A round that missed (broadcasts get lost in bursts, #004) does not drop a service that still answers its
            // polls; when it stops answering, two failed polls start a search of their own.
            show(connView, "поиск в сети не дал ответа, работаю с прежней службой " + svcHost, COLOR_WARN);
            return;
        }
        // Any benchd that answered (unconfirmed, old, foreign) means a PC is on: never two masters of the S3 (D6).
        boolean pcOn = r.pcPresent();
        if (pcOn) {
            stopDirect("в сети отвечает служба benchd (не подтверждена) — напрямую не работаю");
        }
        show(connView, r.line, COLOR_BAD);
        // Direct mode only after two fruitless searches in a row: one round can fall into a burst of lost broadcasts.
        if (!pcOn && direct == null && s3 != null && !s3.isEmpty() && failures >= DIRECT_AFTER_FAILURES) {
            startDirect(s3.get(0).addr);
        }
        nextSearchAt = System.currentTimeMillis() + (direct != null ? DIRECT_SEARCH_MS : BenchFind.backoffMs(failures));
        if (direct == null) {
            setMode(MODE_NONE);
            show(modeView, "ПК не найден — повтор через " + (nextSearchAt - System.currentTimeMillis()) / 1000
                    + " с; кнопка «Найти ПК» — сейчас", COLOR_BAD);
        }
    }

    private void noteS3(List<S3Direct.Seen> s3) {
        s3Seen = new ArrayList<>();
        s3Ms = null;
        for (S3Direct.Seen x : s3) {
            s3Seen.add(x.addr);
            if (s3Ms == null) {
                s3Ms = x.ms;
            }
        }
        if (!s3Seen.isEmpty()) {
            prefs.edit().putString("s3_cache", s3Seen.get(0)).apply();
        }
        showS3();
    }

    private void showS3() {
        if (mode == MODE_DIRECT) {
            return;
        }
        Object addr = BenchTemplate.resolve("s3.addr", status, null);
        Object fnd = BenchTemplate.resolve("s3.found", status, null);
        String[] tc = S3Direct.compare(s3Seen, addr instanceof String ? (String) addr : null,
                mode == MODE_SERVICE && status != null ? Boolean.valueOf(Boolean.TRUE.equals(fnd)) : null, s3Ms);
        show(s3View, tc[0], color(tc[1]));
    }

    /** UQ broadcast while the service is there: read only, to compare the S3's address with /status (D5). */
    private void lookS3() {
        final BenchFind.Binder b = binder();
        final List<String> targets = s3Targets();
        final int port = port("s3_port", S3Direct.TAP_PORT);
        net.execute(new Runnable() {
            @Override
            public void run() {
                final List<S3Direct.Seen> s3 = S3Direct.find(b, targets, port, 3, 900);
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (running) {
                            noteS3(s3);
                        }
                    }
                });
            }
        });
    }

    // --- modes -----------------------------------------------------------------------------------------------------

    private void setMode(int m) {
        boolean changed = m != mode;
        mode = m;
        if (m == MODE_SERVICE) {
            show(modeView, "через службу на ПК " + svcHost + (svcVerified ? "" : " — только просмотр (не подтверждена)"),
                    svcVerified ? COLOR_OK : COLOR_WARN);
        } else if (m == MODE_DIRECT) {
            show(modeView, "напрямую с S3 " + (direct == null ? "" : direct.addr) + " — ПК не найден", COLOR_WARN);
        }
        if (changed) {
            status = null;
            applyMode();
        }
    }

    /** Panels, fields, feeds and buttons of the current mode (the "via" of the spec). */
    private void applyMode() {
        int bit = mode == MODE_DIRECT ? BenchTemplate.VIA_DIRECT : BenchTemplate.VIA_BENCHD;
        for (Map.Entry<View, Integer> e : panelVia.entrySet()) {
            e.getKey().setVisibility((e.getValue() & bit) != 0 ? View.VISIBLE : View.GONE);
        }
        for (Field f : fields) {
            View row = f.list != null ? listViews.get(f.id) : fieldRows.get(f.id);
            if (row != null) {
                row.setVisibility((f.via & bit) != 0 ? View.VISIBLE : View.GONE);
            }
        }
        for (Action a : actions) {
            Button b = actionButtons.get(a.id);
            boolean can = (a.via & bit) != 0;
            b.setAlpha(can ? 1f : 0.4f);
            b.setEnabled(mode == MODE_DIRECT || !Boolean.FALSE.equals(routeOk.get(a.id)));
        }
        for (Feed f : feeds) {
            if ("tail".equals(f.kind)) {
                feedLines.put(f.id, new ArrayList<String>());
                feedAfter.put(f.id, 0L);
                feedViews.get(f.id).setText("");
            }
        }
    }

    private void startDirect(String addr) {
        direct = new DirectRun(addr, port("s3_port", S3Direct.TAP_PORT), port("log_port", S3Direct.LOG_PORT));
        setMode(MODE_DIRECT);
        show(s3View, "S3 " + addr + " найдена, работаю напрямую (лог пишется в файл приложения)", COLOR_WARN);
        new Thread(direct, "bench-direct").start();
    }

    /** Ends direct mode: LOG0 to the S3, the log file closed. why: a line for the screen, or null. */
    private void stopDirect(String why) {
        if (direct == null) {
            return;
        }
        direct.stop = true;
        direct = null;
        if (mode == MODE_DIRECT) {
            mode = MODE_NONE;
            applyMode();
        }
        if (why != null) {
            say(why);
        }
    }

    private interface DirectJob {
        void run(S3Direct.Link link) throws IOException;
    }

    /**
     * Direct mode in its own thread: the only place that talks to the S3 beyond UQ. Every 2 s LOG1 (the S3's log lease is
     * 10 s) and UQ; between them the log datagrams, into the screen and a file; the jobs of sendDirect in between.
     */
    private final class DirectRun implements Runnable {
        final String addr;
        final int tap, logPort;
        volatile boolean stop;
        final LinkedBlockingQueue<DirectJob> jobs = new LinkedBlockingQueue<>();

        DirectRun(String addr, int tap, int logPort) {
            this.addr = addr;
            this.tap = tap;
            this.logPort = logPort;
        }

        @Override
        public void run() {
            final DirectRun self = this;
            S3Direct.State st = new S3Direct.State(addr, S3Direct.LOST_MS);
            SimpleDateFormat iso = new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS", Locale.ROOT);
            Writer file = null;
            final File path = logFile();
            try (S3Direct.Link link = new S3Direct.Link(binder(), addr, tap, logPort)) {
                file = path == null ? null : new OutputStreamWriter(new FileOutputStream(path, true), StandardCharsets.UTF_8);
                long nextLease = 0, nextUq = 0, lastLine = System.currentTimeMillis(), nextHurry = 0;
                while (!stop) {
                    long now = System.currentTimeMillis();
                    if (now >= nextLease) {
                        link.logLease(3, 3);
                        nextLease = now + S3Direct.LEASE_REPEAT_MS;
                    }
                    if (now >= nextUq) {
                        long t0 = System.currentTimeMillis();
                        Map<?, ?> q = link.query();
                        long t1 = System.currentTimeMillis();
                        if (q != null) {
                            st.feedUq(q, t1, t1 - t0, iso.format(new Date()));
                        }
                        nextUq = t1 + 2000;
                        final Map<String, Object> s = st.status(t1);
                        s.put("direct", directInfo(path, st));
                        main.post(new Runnable() {
                            @Override
                            public void run() {
                                if (direct == self) {
                                    renderStatus(s);
                                }
                            }
                        });
                        // The log quiet for 3 s while the S3 answers: somebody (benchd starting?) took it. Look now.
                        if (q != null && now - lastLine > 3000 && now >= nextHurry) {
                            nextHurry = now + 5000;
                            main.post(new Runnable() {
                                @Override
                                public void run() {
                                    if (direct == self) {
                                        search("лог S3 замолчал", false);
                                    }
                                }
                            });
                        }
                    }
                    DirectJob job = jobs.poll();
                    if (job != null && !stop) {
                        job.run(link);
                    }
                    final List<String> lines = new ArrayList<>();
                    for (S3Direct.Rec r : link.readLog(150, st)) {
                        st.feedLine(S3Direct.boardName(r.board), r.text, iso.format(new Date()));
                        lines.add(S3Direct.redact(S3Direct.formatLine(r, new Date())));
                    }
                    if (!lines.isEmpty()) {
                        lastLine = System.currentTimeMillis();
                        if (file != null) {
                            for (String l : lines) {
                                file.write(l);
                                file.write('\n');
                            }
                            file.flush();
                        }
                        main.post(new Runnable() {
                            @Override
                            public void run() {
                                if (direct == self) {
                                    directLines(lines);
                                }
                            }
                        });
                    }
                }
                link.logStop();
            } catch (IOException | RuntimeException e) {
                final String why = e.getClass().getSimpleName() + ": " + e.getMessage();
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (direct == self) {
                            stopDirect("прямой режим остановлен: " + why);
                        }
                    }
                });
            } finally {
                if (file != null) {
                    try {
                        file.close();
                    } catch (IOException ignored) {
                        // nothing more to write
                    }
                }
            }
        }
    }

    private Map<String, Object> directInfo(File path, S3Direct.State st) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("log_file", path == null ? null : path.getName());
        d.put("datagrams", st.datagrams);
        return d;
    }

    /** A new file for this stretch of direct mode; the oldest go when the folder is above 20 MB. */
    private File logFile() {
        File base = getExternalFilesDir(null);
        if (base == null) {
            return null;
        }
        File dir = new File(base, LOG_DIR);
        if (!dir.isDirectory() && !dir.mkdirs()) {
            return null;
        }
        File[] old = dir.listFiles();
        if (old != null) {
            Arrays.sort(old);
            long total = 0;
            for (File f : old) {
                total += f.length();
            }
            for (int k = 0; k < old.length && total > LOG_DIR_MAX; k++) {
                total -= old[k].length();
                if (!old[k].delete()) {
                    break;
                }
            }
        }
        return new File(dir, new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.ROOT).format(new Date()) + ".log");
    }

    private void directLines(List<String> lines) {
        for (Feed f : feeds) {
            if (!"tail".equals(f.kind) || (f.via & BenchTemplate.VIA_DIRECT) == 0) {
                continue;
            }
            CheckBox pause = feedPause.get(f.id);
            if (pause != null && pause.isChecked()) {
                continue;
            }
            List<String> keep = feedLines.get(f.id);
            keep.addAll(lines);
            int max = Math.min(f.keep, 300);
            while (keep.size() > max) {
                keep.remove(0);
            }
            StringBuilder sb = new StringBuilder();
            for (int k = keep.size() - 1; k >= 0; k--) {
                sb.append(keep.get(k)).append('\n');
            }
            feedViews.get(f.id).setText(sb.toString());
        }
    }

    // --- BenchUi: the widgets, in the order of the spec ------------------------------------------------------------

    @Override
    public void header(String title, String template) {
        headerTemplate = template;
        svcView = text("служба: …", 12, false);
        svcView.setTextColor(COLOR_MUTE);
        svcView.setContentDescription("svc");
        root.addView(svcView);
        updatedView = text("", 12, false);
        updatedView.setTextColor(COLOR_MUTE);
        root.addView(updatedView);
    }

    @Override
    public void panel(String id, String title, int via) {
        current = new LinearLayout(this);
        current.setOrientation(LinearLayout.VERTICAL);
        current.setPadding(0, 24, 0, 8);
        TextView h = text(title.toUpperCase(Locale.ROOT), 13, true);
        h.setTextColor(COLOR_MUTE);
        current.addView(h);
        current.setContentDescription("p-" + id);
        root.addView(current);
        panelVia.put(current, via);
        currentRow = null;
    }

    @Override
    public void field(Field f) {
        fields.add(f);
        if (f.list != null) {
            LinearLayout box = new LinearLayout(this);
            box.setOrientation(LinearLayout.VERTICAL);
            listViews.put(f.id, box);
            current.addView(box);
            return;
        }
        LinearLayout row = hbox();
        TextView k = text(f.label, 14, false);
        k.setTextColor(COLOR_MUTE);
        row.addView(k, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        TextView v = text("…", 14, true);
        v.setGravity(Gravity.END);
        v.setContentDescription("f-" + f.id);
        row.addView(v, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1.2f));
        fieldViews.put(f.id, v);
        fieldRows.put(f.id, row);
        current.addView(row);
    }

    @Override
    public void actionRow() {
        currentRow = new LinearLayout(this);
        currentRow.setOrientation(LinearLayout.VERTICAL);
        current.addView(currentRow);
    }

    @Override
    public void action(final Action a) {
        actions.add(a);
        LinearLayout box = hbox();
        box.setGravity(Gravity.CENTER_VERTICAL);
        if (a.params != null) {
            for (Param p : a.params) {
                View w = paramWidget(p, "a-" + a.id + "-" + p.name);
                box.addView(w, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
            }
        }
        Button b = button(a.label, a.danger);
        b.setContentDescription("a-" + a.id);
        b.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                act(a);
            }
        });
        box.addView(b);
        actionViews.put(a.id, box);
        actionButtons.put(a.id, b);
        (currentRow != null ? currentRow : current).addView(box);
    }

    @Override
    public void results() {
        msgView = text("", 14, false);
        msgView.setTextColor(Color.rgb(0x25, 0x63, 0xeb));
        msgView.setContentDescription("msg");
        current.addView(msgView);
        jobView = mono();
        current.addView(jobView);
    }

    @Override
    public void feed(final Feed f) {
        feeds.add(f);
        panel("feed-" + f.id, f.title, f.via);
        LinearLayout filters = hbox();
        if (f.params != null) {
            for (Param p : f.params) {
                View w = paramWidget(p, "q-" + f.id + "-" + p.name);
                if (w instanceof Spinner) {
                    ((Spinner) w).setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
                        private boolean first = true;

                        @Override
                        public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                            if (first) {
                                first = false;
                                return;
                            }
                            resetFeed(f);
                        }

                        @Override
                        public void onNothingSelected(AdapterView<?> parent) {
                        }
                    });
                } else if (w instanceof EditText) {
                    ((EditText) w).setImeOptions(EditorInfo.IME_ACTION_DONE);
                    ((EditText) w).setOnEditorActionListener(new TextView.OnEditorActionListener() {
                        @Override
                        public boolean onEditorAction(TextView v, int actionId, android.view.KeyEvent event) {
                            resetFeed(f);
                            return false;
                        }
                    });
                }
                filters.addView(w, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
            }
        }
        current.addView(filters);
        if ("tail".equals(f.kind)) {
            LinearLayout row = hbox();
            CheckBox pause = new CheckBox(this);
            pause.setText("пауза");
            feedPause.put(f.id, pause);
            row.addView(pause);
            Button clear = button("очистить", false);
            clear.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    feedLines.put(f.id, new ArrayList<String>());
                    feedViews.get(f.id).setText("");
                }
            });
            row.addView(clear);
            current.addView(row);
        }
        TextView rows = mono();
        rows.setContentDescription("feed-" + f.id);
        feedViews.put(f.id, rows);
        feedAfter.put(f.id, 0L);
        feedLines.put(f.id, new ArrayList<String>());
        current.addView(rows);
    }

    // --- widgets ---------------------------------------------------------------------------------------------------

    private TextView text(String s, float sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        if (bold) {
            t.setTypeface(Typeface.DEFAULT_BOLD);
        }
        return t;
    }

    private TextView mono() {
        TextView t = text("", 11, false);
        t.setTypeface(Typeface.MONOSPACE);
        t.setTextIsSelectable(true);
        return t;
    }

    private LinearLayout hbox() {
        LinearLayout l = new LinearLayout(this);
        l.setOrientation(LinearLayout.HORIZONTAL);
        return l;
    }

    private Button button(String label, boolean danger) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        if (danger) {
            b.setTextColor(COLOR_BAD);
        }
        return b;
    }

    private View paramWidget(Param p, String key) {
        View w;
        if ("enum".equals(p.type)) {
            Spinner s = new Spinner(this);
            List<String> labels = new ArrayList<>();
            int sel = 0;
            for (int k = 0; k < p.choices.length; k++) {
                labels.add(p.choices[k][1]);
                if (p.choices[k][0].equals(p.def)) {
                    sel = k;
                }
            }
            ArrayAdapter<String> ad = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, labels);
            ad.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
            s.setAdapter(ad);
            s.setSelection(sel);
            w = s;
        } else {
            EditText e = new EditText(this);
            e.setSingleLine(true);
            e.setHint(p.placeholder == null || p.placeholder.isEmpty() ? p.label : p.placeholder);
            e.setText(p.def);
            if ("int".equals(p.type)) {
                e.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_FLAG_SIGNED);
            }
            w = e;
        }
        w.setContentDescription(key);
        paramViews.put(key, w);
        return w;
    }

    private Map<String, Object> paramValues(String prefix, Param[] params) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (params == null) {
            return out;
        }
        for (Param p : params) {
            View w = paramViews.get(prefix + "-" + p.name);
            Object v = p.def;
            if (w instanceof Spinner) {
                v = p.choices[((Spinner) w).getSelectedItemPosition()][0];
            } else if (w instanceof EditText) {
                v = ((EditText) w).getText().toString();
            }
            if ("int".equals(p.type) && !"".equals(v)) {
                try {
                    v = Long.parseLong((String) v);
                } catch (NumberFormatException e) {
                    v = "";
                }
            }
            out.put(p.name, v);
        }
        return out;
    }

    private void say(String s) {
        if (msgView != null) {
            msgView.setText(s);
        }
        if (barView != null) {
            barView.setText(s);
        }
    }

    /** Text and colour only when they change: fewer redraws, and uiautomator sees the screen idle sooner. */
    private static void show(TextView t, CharSequence text, int color) {
        if (!text.toString().equals(t.getText().toString())) {
            t.setText(text);
        }
        if (color != 0 && t.getCurrentTextColor() != color) {
            t.setTextColor(color);
        }
    }

    private static int color(String c) {
        if ("ok".equals(c)) {
            return COLOR_OK;
        }
        if ("warn".equals(c)) {
            return COLOR_WARN;
        }
        if ("bad".equals(c)) {
            return COLOR_BAD;
        }
        return Color.DKGRAY;
    }

    // --- rendering --------------------------------------------------------------------------------------------------

    private void renderStatus(Object st) {
        status = st;
        boolean dir = mode == MODE_DIRECT;
        show(svcView, BenchTemplate.render(dir ? BenchSpec.text("direct_header") : headerTemplate, st, vars), 0);
        Map<String, Object> v = new HashMap<>(vars);
        v.put("time", new SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(new Date()));
        show(updatedView, BenchTemplate.render(BenchSpec.text("updated"), st, v), 0);
        int bit = dir ? BenchTemplate.VIA_DIRECT : BenchTemplate.VIA_BENCHD;
        for (Field f : fields) {
            if ((f.via & bit) == 0) {
                continue;
            }
            if (f.list != null) {
                LinearLayout box = listViews.get(f.id);
                Object items = BenchTemplate.resolve(f.list, st, vars);
                String sig = BenchTemplate.jsonStr(items);
                if (sig.equals(box.getTag())) {
                    continue;
                }
                box.setTag(sig);
                box.removeAllViews();
                if (items instanceof List) {
                    for (Object it : (List<?>) items) {
                        String[] tc = BenchTemplate.pick(f.cases, it, vars);
                        LinearLayout row = hbox();
                        TextView k = text(BenchTemplate.render(f.label, it, vars), 14, false);
                        k.setTextColor(COLOR_MUTE);
                        row.addView(k, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
                        TextView val = text(tc[0], 14, true);
                        val.setTextColor(color(tc[1]));
                        row.addView(val);
                        box.addView(row);
                    }
                }
            } else {
                String[] tc = BenchTemplate.pick(f.cases, st, vars);
                show(fieldViews.get(f.id), tc[0], color(tc[1]));
            }
        }
        for (Action a : actions) {
            actionViews.get(a.id).setVisibility(BenchTemplate.holds(a.visibleIf, st, vars) ? View.VISIBLE : View.GONE);
        }
        if (dir) {
            Object addr = BenchTemplate.resolve("s3.addr", st, null), ms = BenchTemplate.resolve("s3.answer_ms", st, null);
            Object file = BenchTemplate.resolve("direct.log_file", st, null);
            show(s3View, "S3 " + addr + (ms == null ? " не отвечает на UQ" : ", ответ " + ms + " мс")
                    + (file == null ? "" : "; лог в файл " + file), ms == null ? COLOR_BAD : COLOR_OK);
        } else {
            showS3();
        }
    }

    // --- HTTP --------------------------------------------------------------------------------------------------------

    private static final class Reply {
        int code;
        Object data;
        String error;
    }

    /** One request in a worker thread; the reply (or a readable error) on the main thread. */
    private void call(final String method, final String path, final Map<String, Object> body, final Callback cb) {
        if (mode != MODE_SERVICE || svcHost == null) {
            cb.done(errorReply(mode == MODE_DIRECT ? BenchSpec.text("direct_unavailable")
                    : "ПК не найден: нажмите «Найти ПК» или введите адрес вручную"));
            return;
        }
        final String b = "http://" + svcHost;
        // The token only to a service that signed our nonce with it (D2).
        final String token = svcVerified ? token() : "";
        final BenchFind.Binder bind = binder();
        net.execute(new Runnable() {
            @Override
            public void run() {
                final Reply r = http(bind, b, method, path, body, token);
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        cb.done(r);
                    }
                });
            }
        });
    }

    private interface Callback {
        void done(Reply r);
    }

    private static Reply errorReply(String why) {
        Reply r = new Reply();
        r.code = -1;
        r.error = why;
        return r;
    }

    private static Reply http(BenchFind.Binder bind, String base, String method, String path, Map<String, Object> body,
                              String token) {
        String host = base.substring("http://".length());
        HttpURLConnection c = null;
        try {
            c = bind.open(new URL(base + path));
            c.setConnectTimeout(3000);
            c.setReadTimeout("POST".equals(method) ? 30000 : 8000);
            c.setRequestMethod(method);
            c.setUseCaches(false);
            if ("POST".equals(method)) {
                c.setDoOutput(true);
                c.setRequestProperty("Content-Type", "application/json");
                c.setRequestProperty("X-Bench", "1");
                if (token != null && !token.isEmpty()) {
                    c.setRequestProperty("X-Bench-Token", token);
                }
                byte[] data = BenchTemplate.jsonStr(body).getBytes(StandardCharsets.UTF_8);
                try (OutputStream out = c.getOutputStream()) {
                    out.write(data);
                }
            }
            Reply r = new Reply();
            r.code = c.getResponseCode();
            InputStream in = r.code >= 400 ? c.getErrorStream() : c.getInputStream();
            String text = in == null ? "" : new String(readAll(in), StandardCharsets.UTF_8);
            try {
                r.data = text.isEmpty() ? new LinkedHashMap<String, Object>() : MiniJson.parse(text);
            } catch (IllegalArgumentException e) {
                r.data = new LinkedHashMap<String, Object>();
            }
            if (r.code == 401) {
                r.error = token == null || token.isEmpty()
                        ? "служба не подтвердила токен — команды недоступны (bench.py phone-link на своём ПК)"
                        : "токен не принят службой: bench.py phone-link на ПК";
            }
            return r;
        } catch (ConnectException e) {
            return errorReply("служба benchd не отвечает на ПК " + host);
        } catch (SocketTimeoutException | NoRouteToHostException | UnknownHostException e) {
            return errorReply("ПК " + host + " не отвечает");
        } catch (IOException | RuntimeException e) {
            return errorReply("сеть: " + e.getClass().getSimpleName() + ": " + e.getMessage());
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            out.write(buf, 0, n);
        }
        return out.toByteArray();
    }

    // --- polling -----------------------------------------------------------------------------------------------------

    private void refreshNow() {
        if (mode != MODE_SERVICE) {
            failures = 0;
            search("кнопка «Обновить»", false);
            return;
        }
        checkRoutes();
        refreshStatus();
        for (Feed f : feeds) {
            if ((f.via & BenchTemplate.VIA_BENCHD) == 0) {
                continue;
            }
            if ("events".equals(f.kind)) {
                refreshFeed(f);
            } else {
                resetFeed(f);
            }
        }
    }

    private void refreshStatus() {
        call("GET", "/status", null, new Callback() {
            @Override
            public void done(Reply r) {
                if (mode != MODE_SERVICE) {
                    return;
                }
                if (r.error != null || r.code != 200) {
                    show(connView, r.error != null ? r.error : BenchSpec.text("no_service") + " (" + r.code + ")", COLOR_BAD);
                    svcView.setText(BenchSpec.text("no_service"));
                    pollErrors++;
                    if (BenchFind.searchAfterErrors(pollErrors)) {
                        pollErrors = 0;
                        setMode(MODE_NONE);
                        search("служба не отвечает", false);
                    }
                    return;
                }
                pollErrors = 0;
                renderStatus(r.data);
            }
        });
    }

    private void refreshJob() {
        if (lastJob == null) {
            return;
        }
        call("GET", "/jobs/" + lastJob, null, new Callback() {
            @Override
            public void done(Reply r) {
                if (r.error != null || r.code != 200 || jobView == null) {
                    return;
                }
                boolean ended = BenchTemplate.resolve("ended", r.data, null) != null;
                Map<String, Object> v = new HashMap<>(vars);
                v.put("state", ended ? BenchTemplate.render(BenchSpec.text("job_ended"), r.data, vars)
                        : BenchSpec.text("job_running"));
                Object tail = BenchTemplate.resolve("tail", r.data, null);
                jobView.setText(BenchTemplate.render(BenchSpec.text("job_state"), r.data, v) + "\n"
                        + (tail instanceof String ? tail : ""));
                if (ended) {
                    lastJob = null;
                }
            }
        });
    }

    private void checkRoutes() {
        call("GET", "/api/routes", null, new Callback() {
            @Override
            public void done(Reply r) {
                if (r.error != null || r.code != 200 || mode != MODE_SERVICE) {
                    return;
                }
                List<String> have = new ArrayList<>();
                Object rows = BenchTemplate.resolve("routes", r.data, null);
                if (rows instanceof List) {
                    for (Object row : (List<?>) rows) {
                        have.add(BenchTemplate.resolve("method", row, null) + " " + BenchTemplate.resolve("path", row, null));
                    }
                }
                Object names = BenchTemplate.resolve("actions", r.data, null);
                for (Action a : actions) {
                    Object name = BenchTemplate.resolve("name", MiniJson.parse(a.bodyJson), null);
                    boolean ok = have.contains(a.method + " " + a.endpoint)
                            && (name == null || (names instanceof Map && ((Map<?, ?>) names).containsKey(name)));
                    routeOk.put(a.id, ok);
                    actionButtons.get(a.id).setEnabled(ok);
                }
            }
        });
    }

    private String query(Feed f, Map<String, Object> extra) {
        Map<String, Object> q = new LinkedHashMap<>();
        Object fixed = MiniJson.parse(f.fixedJson);
        if (fixed instanceof Map) {
            for (Map.Entry<?, ?> e : ((Map<?, ?>) fixed).entrySet()) {
                q.put(String.valueOf(e.getKey()), e.getValue());
            }
        }
        for (Map.Entry<String, Object> e : paramValues("q-" + f.id, f.params).entrySet()) {
            if (e.getValue() != null && !"".equals(e.getValue())) {
                q.put(e.getKey(), e.getValue());
            }
        }
        q.putAll(extra);
        StringBuilder b = new StringBuilder(f.endpoint);
        char sep = '?';
        try {
            for (Map.Entry<String, Object> e : q.entrySet()) {
                b.append(sep).append(URLEncoder.encode(e.getKey(), "UTF-8")).append('=')
                        .append(URLEncoder.encode(BenchTemplate.valueStr(e.getValue()), "UTF-8"));
                sep = '&';
            }
        } catch (java.io.UnsupportedEncodingException e) {
            throw new IllegalStateException(e);
        }
        return b.toString();
    }

    private void resetFeed(Feed f) {
        feedAfter.put(f.id, 0L);
        feedLines.put(f.id, new ArrayList<String>());
        feedViews.get(f.id).setText("");
        if (mode == MODE_SERVICE) {
            refreshFeed(f);
        }
    }

    private void refreshFeed(final Feed f) {
        final TextView view = feedViews.get(f.id);
        if ("events".equals(f.kind)) {
            call("GET", query(f, new HashMap<String, Object>()), null, new Callback() {
                @Override
                public void done(Reply r) {
                    if (r.error != null || r.code != 200) {
                        return;
                    }
                    Object evs = BenchTemplate.resolve("events", r.data, null);
                    if (!(evs instanceof List)) {
                        return;
                    }
                    Pattern bad = Pattern.compile(f.bad == null ? "^$" : f.bad);
                    List<?> list = (List<?>) evs;
                    SpannableStringBuilder sb = new SpannableStringBuilder();
                    for (int k = list.size() - 1; k >= 0; k--) {
                        Object e = list.get(k);
                        if (!(e instanceof Map)) {
                            continue;
                        }
                        Map<?, ?> m = (Map<?, ?>) e;
                        String t = String.valueOf(m.get("t"));
                        String type = String.valueOf(m.get("type"));
                        StringBuilder rest = new StringBuilder();
                        for (Map.Entry<?, ?> kv : m.entrySet()) {
                            if (!contains(f.hideKeys, String.valueOf(kv.getKey()))) {
                                rest.append(' ').append(kv.getKey()).append('=').append(BenchTemplate.valueStr(kv.getValue()));
                            }
                        }
                        int start = sb.length();
                        sb.append(t.length() >= 19 ? t.substring(11, 19) : t).append(' ').append(type);
                        if (bad.matcher(type + (m.get("action") == null ? "" : m.get("action"))).find()) {
                            sb.setSpan(new ForegroundColorSpan(COLOR_BAD), start, sb.length(), Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
                        }
                        Object board = m.get("board");
                        sb.append(board == null ? "" : " " + board).append(rest).append('\n');
                        if (k < list.size() - 200) {
                            break;
                        }
                    }
                    if (!sb.toString().equals(view.getText().toString())) {
                        view.setText(sb);
                    }
                }
            });
            return;
        }
        CheckBox pause = feedPause.get(f.id);
        if (pause != null && pause.isChecked()) {
            return;
        }
        final long after = feedAfter.get(f.id);
        Map<String, Object> extra = new HashMap<>();
        extra.put(f.cursor, after);
        extra.put(f.limitParam, (long) (after > 0 ? f.limit : f.firstLimit));
        call("GET", query(f, extra), null, new Callback() {
            @Override
            public void done(Reply r) {
                if (r.error != null || mode != MODE_SERVICE) {
                    return;
                }
                Object err = BenchTemplate.resolve("error", r.data, null);
                if (err != null) {
                    say("фильтр: " + err);
                    return;
                }
                Object last = BenchTemplate.resolve("last_id", r.data, null);
                Object lines = BenchTemplate.resolve("lines", r.data, null);
                if (feedAfter.get(f.id) != after) {
                    return;           // the filter changed while this was on its way
                }
                if (BenchTemplate.isNum(last)) {
                    feedAfter.put(f.id, ((Number) last).longValue());
                }
                if (lines instanceof List && !((List<?>) lines).isEmpty()) {
                    List<String> keep = feedLines.get(f.id);
                    for (Object ln : (List<?>) lines) {
                        keep.add(String.valueOf(ln));
                    }
                    int max = Math.min(f.keep, 300);       // a phone screen: the newest 300 of them
                    while (keep.size() > max) {
                        keep.remove(0);
                    }
                    StringBuilder sb = new StringBuilder();
                    for (int k = keep.size() - 1; k >= 0; k--) {
                        sb.append(keep.get(k)).append('\n');
                    }
                    view.setText(sb.toString());
                }
            }
        });
    }

    private static boolean contains(String[] a, String s) {
        if (a == null) {
            return false;
        }
        for (String x : a) {
            if (x.equals(s)) {
                return true;
            }
        }
        return false;
    }

    // --- actions -----------------------------------------------------------------------------------------------------

    private void act(final Action a) {
        boolean dir = mode == MODE_DIRECT;
        if (dir && (a.via & BenchTemplate.VIA_DIRECT) == 0) {
            say(a.directUnavailable != null ? a.directUnavailable : BenchSpec.text("direct_unavailable"));
            return;
        }
        if (status == null) {
            say(mode == MODE_NONE ? "ПК и S3 не найдены" : BenchSpec.text("no_service"));
            return;
        }
        final Map<String, Object> v = new HashMap<>(vars);
        final Map<String, Object> param = paramValues("a-" + a.id, a.params);
        v.put("param", param);
        if (!BenchTemplate.holds(a.availableIf, status, v)) {
            say(BenchTemplate.render(a.unavailableText, status, v));
            return;
        }
        final List<String> questions = new ArrayList<>();
        if (a.confirm != null) {
            questions.add(BenchTemplate.render(a.confirm, status, v));
        }
        if (a.confirm2 != null) {
            questions.add(BenchTemplate.render(a.confirm2, status, v));
        }
        if (dir) {
            ask(questions, 0, new Runnable() {
                @Override
                public void run() {
                    sendDirect(a, v);
                }
            });
            return;
        }
        final Map<String, Object> body = BenchTemplate.renderBody(a.bodyJson, status, v);
        body.putAll(param);
        body.put("owner", BenchSpec.OWNER);
        Object holder = BenchTemplate.resolve("lock.holder.owner", status, null);
        if (a.requiresLock && holder != null && !BenchSpec.OWNER.equals(holder)) {
            questions.add(BenchTemplate.render(BenchSpec.text("lock_other"), status, v));
            body.put("force", Boolean.TRUE);
        }
        ask(questions, 0, new Runnable() {
            @Override
            public void run() {
                send(a, body, v);
            }
        });
    }

    /** The confirmations one after the other; "Нет" on any of them stops the action. */
    private void ask(final List<String> questions, final int k, final Runnable then) {
        if (k >= questions.size()) {
            then.run();
            return;
        }
        new AlertDialog.Builder(this)
                .setMessage(questions.get(k))
                .setPositiveButton("Да", new android.content.DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(android.content.DialogInterface d, int w) {
                        ask(questions, k + 1, then);
                    }
                })
                .setNegativeButton("Нет", null)
                .show();
    }

    /**
     * An action in direct mode (D6): first a fresh search for benchd - when any benchd answers, nothing is sent to the S3
     * and the screen goes back to the service; else the command goes into the direct thread (UC / UX / UQ).
     */
    private void sendDirect(final Action a, final Map<String, Object> v) {
        final DirectRun run = direct;
        if (run == null) {
            say(BenchSpec.text("direct_unavailable"));
            return;
        }
        final String[] cmd;
        try {
            if ("uc".equals(a.directOp)) {
                cmd = new String[]{"uc", BenchTemplate.render(a.directText, null, v)};
            } else if ("ux".equals(a.directOp)) {
                cmd = new String[]{"ux", null};
            } else if ("cmd".equals(a.directOp)) {
                Map<?, ?> p = (Map<?, ?>) v.get("param");
                cmd = S3Direct.directCommand(String.valueOf(p.get("target")), String.valueOf(p.get("cmd")));
            } else {
                say("неизвестное действие прямого режима: " + a.directOp);
                return;
            }
        } catch (IllegalArgumentException e) {
            say(e.getMessage());
            return;
        }
        say("проверяю, не включился ли ПК…");
        final BenchFind.Config cfg = config(false);
        final BenchFind.Binder b = binder();
        net.execute(new Runnable() {
            @Override
            public void run() {
                final BenchFind.Result r = BenchFind.find(b, cfg, rnd);
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (r.pcPresent() || r.chosen != null) {
                            say(BenchSpec.text("direct_pc_back"));
                            found(r, null, cfg);
                            return;
                        }
                        if (direct != run) {
                            say(BenchSpec.text("direct_unavailable"));
                            return;
                        }
                        say(BenchSpec.text("busy"));
                        run.jobs.add(new DirectJob() {
                            @Override
                            public void run(S3Direct.Link link) throws IOException {
                                S3Direct.Reply rep;
                                if ("uq".equals(cmd[0])) {
                                    Map<?, ?> q = link.query();
                                    rep = new S3Direct.Reply();
                                    rep.status = q == null ? -1 : 0;
                                    rep.text = q == null ? "" : BenchTemplate.jsonStr(q);
                                } else if ("ux".equals(cmd[0])) {
                                    rep = link.ux();
                                } else {
                                    rep = link.uc(cmd[1]);
                                }
                                final S3Direct.Reply fr = rep;
                                main.post(new Runnable() {
                                    @Override
                                    public void run() {
                                        directReply(fr);
                                    }
                                });
                            }
                        });
                    }
                });
            }
        });
    }

    private void directReply(S3Direct.Reply r) {
        Map<String, Object> v = new HashMap<>(vars);
        if (r.status < 0) {
            v.put("why", r.text.isEmpty() ? "нет ответа за " + r.attempts + " попыток" : r.text);
            say(BenchTemplate.render(BenchSpec.text("direct_failed"), null, v));
            return;
        }
        String head = r.status == 0 ? "" : r.status == 1 ? "неизвестная команда: " : r.status == 3
                ? "линия к LyraT выключена (no_tx): " : "статус " + r.status + ": ";
        v.put("reply", head + r.text.trim());
        say(BenchTemplate.render(BenchSpec.text("direct_done"), null, v));
        if (jobView != null) {
            jobView.setText(r.text);
        }
    }

    private void send(final Action a, Map<String, Object> body, final Map<String, Object> v) {
        say(BenchSpec.text("busy"));
        call(a.method, a.endpoint, body, new Callback() {
            @Override
            public void done(Reply r) {
                if (r.code < 0) {
                    say(r.error);
                    return;
                }
                Map<String, Object> rv = new HashMap<>(v);
                rv.put("code", (long) r.code);
                rv.put("json", BenchTemplate.jsonStr(r.data));
                Object id = BenchTemplate.resolve("id", r.data, null);
                if (r.code == 202 && id != null) {
                    lastJob = String.valueOf(id);
                    refreshJob();
                }
                boolean ok = r.code == 200 || r.code == 202;
                if (r.error != null) {
                    say(r.error);
                } else if (a.ok != null || a.fail != null) {
                    say(BenchTemplate.pick(ok ? a.ok : a.fail, r.data, rv)[0]);
                } else if (r.code == 202) {
                    say(BenchTemplate.render(BenchSpec.text("job_started"), r.data, rv));
                } else {
                    say(BenchTemplate.render(BenchSpec.text(r.code == 200 ? "done" : "refused"), r.data, rv));
                }
                refreshStatus();
                for (Feed f : feeds) {
                    if ("events".equals(f.kind)) {
                        refreshFeed(f);
                    }
                }
            }
        });
    }
}

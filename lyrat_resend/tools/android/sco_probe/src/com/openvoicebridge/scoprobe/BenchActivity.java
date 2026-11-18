package com.openvoicebridge.scoprobe;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
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
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.NoRouteToHostException;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.URLEncoder;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

/**
 * The "Стенд" screen (goal 104): the bench service benchd on the PC, controlled from the phone over the Wi-Fi.
 * What is on it comes from the generated BenchSpec (bench_ui_spec.json, the same spec as benchd's web page); this class
 * is the hand-written runtime: native widgets for the calls of BenchSpec.build(), HTTP to benchd, polling, confirmations,
 * network errors, the PC's address and the LAN token.
 *
 * Address and token: typed in, or found ("Найти ПК": UDP "BENCHD?" broadcast, benchd --lan answers), or handed over by
 * `bench.py phone-link` as files/bench_link.json in the app's external files dir (only adb and this app can write it),
 * which is read once and deleted. Kept in the app's private preferences. No intent extra carries the token.
 */
public class BenchActivity extends Activity implements BenchUi {
    static final String LINK_FILE = "bench_link.json";
    private static final int DISCOVERY_PORT = 5082;
    private static final int COLOR_OK = Color.rgb(0x18, 0x79, 0x4e);
    private static final int COLOR_WARN = Color.rgb(0xa1, 0x62, 0x07);
    private static final int COLOR_BAD = Color.rgb(0xc2, 0x41, 0x0c);
    private static final int COLOR_MUTE = Color.rgb(0x66, 0x70, 0x7c);

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService net = Executors.newFixedThreadPool(3);
    private SharedPreferences prefs;
    private LinearLayout root;
    private LinearLayout current;          // the panel being built
    private LinearLayout currentRow;
    private EditText hostEdit, tokenEdit;
    private TextView connView, svcView, updatedView, msgView, jobView, barView;
    private final Map<String, Object> vars = new HashMap<>();
    private final List<Field> fields = new ArrayList<>();
    private final Map<String, TextView> fieldViews = new HashMap<>();
    private final Map<String, LinearLayout> listViews = new HashMap<>();
    private final List<Action> actions = new ArrayList<>();
    private final Map<String, View> actionViews = new HashMap<>();
    private final Map<String, Button> actionButtons = new HashMap<>();
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

    // --- life ------------------------------------------------------------------------------------------------------

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("bench", MODE_PRIVATE);
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
        buildConnection();
        BenchSpec.build(this);
        importLink();
    }

    @Override
    protected void onResume() {
        super.onResume();
        importLink();
        running = true;
        checkRoutes();
        tick.run();
        for (final Feed f : feeds) {
            schedule(f);
        }
    }

    @Override
    protected void onPause() {
        running = false;
        main.removeCallbacksAndMessages(null);
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
            refreshStatus();
            refreshJob();
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
                refreshFeed(f);
                main.postDelayed(this, f.pollMs);
            }
        }, 300);
    }

    // --- connection: address, token, finding the PC ----------------------------------------------------------------

    private void buildConnection() {
        TextView title = text(BenchSpec.TITLE, 20, true);
        root.addView(title);
        LinearLayout row = hbox();
        hostEdit = new EditText(this);
        hostEdit.setHint("адрес ПК: IP:5080");
        hostEdit.setSingleLine(true);
        hostEdit.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        hostEdit.setText(prefs.getString("host", ""));
        hostEdit.setContentDescription("host");
        row.addView(hostEdit, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        Button find = button("Найти ПК", false);
        find.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                discover();
            }
        });
        row.addView(find);
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
                saveConnection(hostEdit.getText().toString().trim(), tokenEdit.getText().toString().trim());
                say("адрес и токен сохранены");
                refreshNow();
            }
        });
        row.addView(save);
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
        connView = text("", 13, false);
        connView.setTextColor(COLOR_BAD);
        connView.setContentDescription("conn");
        root.addView(connView);
    }

    private void saveConnection(String host, String token) {
        prefs.edit().putString("host", host).putString("token", token).apply();
        hostEdit.setText(host);
        tokenEdit.setText(token);
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
            Object host = BenchTemplate.resolve("host", o, null), token = BenchTemplate.resolve("token", o, null);
            if (host instanceof String) {
                saveConnection((String) host, token instanceof String ? (String) token : "");
                say("адрес и токен получены с ПК");
            }
        } catch (IOException | RuntimeException e) {
            say("файл связи с ПК не прочитан: " + e.getMessage());
        } finally {
            if (!f.delete()) {
                say("файл связи с ПК не удалён");
            }
        }
    }

    private void discover() {
        say("ищу службу benchd в сети…");
        final String typed = hostEdit.getText().toString().trim();
        net.execute(new Runnable() {
            @Override
            public void run() {
                final String found = broadcastFind(typed);
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (found == null) {
                            say("ПК не найден: benchd запущен с --lan? та же Wi-Fi? брандмауэр пускает UDP " + DISCOVERY_PORT + "?");
                        } else {
                            saveConnection(found, tokenEdit.getText().toString().trim());
                            say("найден ПК " + found);
                            refreshNow();
                        }
                    }
                });
            }
        });
    }

    /** "host:port" of the first benchd that answers "BENCHD?", or null. Also asks the port beside a typed one. */
    private static String broadcastFind(String typed) {
        try (DatagramSocket s = new DatagramSocket()) {
            s.setBroadcast(true);
            s.setSoTimeout(2500);
            byte[] q = "BENCHD?".getBytes(StandardCharsets.US_ASCII);
            InetAddress all = InetAddress.getByName("255.255.255.255");
            List<Integer> ports = new ArrayList<>();
            ports.add(DISCOVERY_PORT);
            int colon = typed.lastIndexOf(':');
            if (colon > 0) {
                try {
                    int http = Integer.parseInt(typed.substring(colon + 1));
                    if (http != 5080) {
                        ports.add(http + 2);     // a benchd on a port offset answers there (5080 -> 5082)
                    }
                } catch (NumberFormatException ignored) {
                    // a typed address without a port
                }
            }
            for (int p : ports) {
                s.send(new DatagramPacket(q, q.length, all, p));
            }
            byte[] buf = new byte[512];
            DatagramPacket ans = new DatagramPacket(buf, buf.length);
            s.receive(ans);
            Object o = MiniJson.parse(new String(ans.getData(), 0, ans.getLength(), StandardCharsets.UTF_8));
            Object port = BenchTemplate.resolve("http_port", o, null);
            if (!BenchTemplate.isNum(port)) {
                return null;
            }
            return ans.getAddress().getHostAddress() + ":" + ((Number) port).longValue();
        } catch (IOException | RuntimeException e) {
            return null;
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
    public void panel(String id, String title) {
        current = new LinearLayout(this);
        current.setOrientation(LinearLayout.VERTICAL);
        current.setPadding(0, 24, 0, 8);
        TextView h = text(title.toUpperCase(Locale.ROOT), 13, true);
        h.setTextColor(COLOR_MUTE);
        current.addView(h);
        root.addView(current);
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
        panel("feed-" + f.id, f.title);
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
        show(svcView, BenchTemplate.render(headerTemplate, st, vars), 0);
        Map<String, Object> v = new HashMap<>(vars);
        v.put("time", new SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(new Date()));
        show(updatedView, BenchTemplate.render(BenchSpec.text("updated"), st, v), 0);
        for (Field f : fields) {
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
    }

    // --- HTTP --------------------------------------------------------------------------------------------------------

    private static final class Reply {
        int code;
        Object data;
        String error;
    }

    private String base() {
        String h = prefs.getString("host", "").trim();
        if (h.isEmpty()) {
            return null;
        }
        if (!h.startsWith("http://")) {
            h = "http://" + h;
        }
        if (h.lastIndexOf(':') <= 5) {
            h = h + ":5080";
        }
        return h;
    }

    /** One request in a worker thread; the reply (or a readable error) on the main thread. */
    private void call(final String method, final String path, final Map<String, Object> body, final Callback cb) {
        final String b = base();
        if (b == null) {
            cb.done(errorReply("нет адреса ПК: введите его, нажмите «Найти ПК» или bench.py phone-link"));
            return;
        }
        final String token = prefs.getString("token", "");
        net.execute(new Runnable() {
            @Override
            public void run() {
                final Reply r = http(b, method, path, body, token);
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

    private static Reply http(String base, String method, String path, Map<String, Object> body, String token) {
        String host = base.substring("http://".length());
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + path).openConnection();
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
                r.error = "нужен токен управления (или он неверный): bench.py phone-link на ПК, или введите его выше";
            }
            return r;
        } catch (ConnectException e) {
            return errorReply("служба benchd не запущена на ПК " + host + " (или не с --lan)");
        } catch (SocketTimeoutException | NoRouteToHostException | UnknownHostException e) {
            return errorReply("ПК " + host + " не отвечает: телефон в той же Wi-Fi? адрес верный? брандмауэр Windows пускает python?");
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
        checkRoutes();
        refreshStatus();
        for (Feed f : feeds) {
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
                if (r.error != null || r.code != 200) {
                    connView.setText(r.error != null ? r.error : BenchSpec.text("no_service") + " (" + r.code + ")");
                    svcView.setText(BenchSpec.text("no_service"));
                    return;
                }
                show(connView, "", 0);
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
                if (r.error != null || r.code != 200) {
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
        refreshFeed(f);
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
                if (r.error != null) {
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
        if (status == null) {
            say(BenchSpec.text("no_service"));
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

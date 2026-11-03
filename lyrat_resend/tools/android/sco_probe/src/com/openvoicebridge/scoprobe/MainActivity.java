package com.openvoicebridge.scoprobe;

import android.app.Activity;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothHeadset;
import android.bluetooth.BluetoothManager;
import android.bluetooth.BluetoothProfile;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AudioEffect;
import android.media.audiofx.AutomaticGainControl;
import android.media.audiofx.NoiseSuppressor;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.WindowManager;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;

/**
 * Bench tool: opens the Bluetooth headset audio channel without a call, driven by `am start`.
 *
 *   adb shell am start -n com.openvoicebridge.scoprobe/.MainActivity --es cmd CMD [--es id ID] ...
 *
 *   open   [--es device LyraT_Phone]     MODE_IN_COMMUNICATION + setCommunicationDevice(BLUETOOTH_SCO)
 *   close                                release the route, back to MODE_NORMAL
 *   play   --es tone 1000[,1800] [--ei seconds 5] [--ei level 8000]    sum of sines over the open route
 *   play   --es wav NAME.wav             16 bit PCM mono from the files dir, at the file's sample rate
 *   record --ei seconds 5 [--ei rate 16000] [--es out NAME.wav] [--es source vc|mic|recog|raw] [--ei fx 1]
 *                                        WAV from the headset into the files dir; fx 0 disables AEC, NS, AGC
 *   status                               refresh status.json
 *
 * The result of every command is in status.json in getExternalFilesDir(null) and in logcat, tag ScoProbe.
 * It carries no Bluetooth address. Only the device named in `open` is ever routed to. The SCO port hides
 * its identity on this ROM (the phone model as its name, a zero address), so the route is opened only
 * while the named device is the one and only connected headset, and confirmed by the headset profile.
 */
public class MainActivity extends Activity {
    private static final String TAG = "ScoProbe";
    private static final String DEFAULT_DEVICE = "LyraT_Phone";
    private static final int TRACK_RATE = 16000;
    private static final int RECORD_RATE = 16000;
    private static final int DEFAULT_LEVEL = 8000;       // about -12 dBFS, leaves headroom for tone sums
    private static final int MAX_SECONDS = 3600;

    private final Handler main = new Handler(Looper.getMainLooper());
    private AudioManager am;
    private TextView view;
    private File filesDir;

    // Everything below is touched on the main thread only, the worker threads post back to it.
    private String lastCmd = "none";
    private String lastId = "";
    private boolean lastOk = true;
    private String lastError = null;
    private boolean opened = false;
    private String wantedDevice = null;
    private long openRequestedAt = 0;
    private long openLatencyMs = -1;
    private String scoState = "UNKNOWN";
    private int scoStateChanges = 0;
    private int routeLosses = 0;
    private AudioFocusRequest focus;
    private BluetoothHeadset headset;
    private BluetoothDevice routeTarget;
    private final ArrayList<Intent> waitingForHeadset = new ArrayList<>();
    private JSONObject lastPlay = null;
    private JSONObject lastRecord = null;
    private int commandsHandled = 0;

    private OutputLoop output;
    private RecordLoop recorder;

    private final BroadcastReceiver scoReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            int state = intent.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE, AudioManager.SCO_AUDIO_STATE_ERROR);
            scoState = scoStateName(state);
            scoStateChanges++;
            updateRouteProgress();
            writeStatus();
        }
    };

    private final AudioManager.OnCommunicationDeviceChangedListener routeListener =
            new AudioManager.OnCommunicationDeviceChangedListener() {
                @Override
                public void onCommunicationDeviceChanged(AudioDeviceInfo device) {
                    if (opened && openLatencyMs >= 0 && !isScoRoute(device)) {
                        routeLosses++;
                    }
                    updateRouteProgress();
                    writeStatus();
                }
            };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        am = (AudioManager) getSystemService(AUDIO_SERVICE);
        filesDir = getExternalFilesDir(null);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        view = new TextView(this);
        view.setPadding(32, 96, 32, 32);
        view.setTextSize(13f);
        setContentView(view);
        registerReceiver(scoReceiver, new IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED));
        am.addOnCommunicationDeviceChangedListener(getMainExecutor(), routeListener);
        BluetoothManager bt = (BluetoothManager) getSystemService(BLUETOOTH_SERVICE);
        bt.getAdapter().getProfileProxy(this, headsetListener, BluetoothProfile.HEADSET);
        handle(getIntent());
    }

    private final BluetoothProfile.ServiceListener headsetListener = new BluetoothProfile.ServiceListener() {
        @Override
        public void onServiceConnected(int profile, BluetoothProfile proxy) {
            headset = (BluetoothHeadset) proxy;
            ArrayList<Intent> queued = new ArrayList<>(waitingForHeadset);
            waitingForHeadset.clear();
            for (Intent intent : queued) {
                handle(intent);
            }
        }

        @Override
        public void onServiceDisconnected(int profile) {
            headset = null;
        }
    };

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handle(intent);
    }

    @Override
    protected void onDestroy() {
        doClose();
        am.removeOnCommunicationDeviceChangedListener(routeListener);
        unregisterReceiver(scoReceiver);
        if (headset != null) {
            BluetoothManager bt = (BluetoothManager) getSystemService(BLUETOOTH_SERVICE);
            bt.getAdapter().closeProfileProxy(BluetoothProfile.HEADSET, headset);
        }
        super.onDestroy();
    }

    private void handle(Intent intent) {
        String cmd = intent == null ? null : intent.getStringExtra("cmd");
        if (cmd == null) {
            writeStatus();
            return;
        }
        // The proxy arrives a moment after onCreate and `open` needs it; commands keep their order.
        if (headset == null) {
            waitingForHeadset.add(intent);
            return;
        }
        // The same intent may be seen again if the activity is ever recreated.
        intent.removeExtra("cmd");
        lastCmd = cmd;
        lastId = intent.getStringExtra("id") == null ? "" : intent.getStringExtra("id");
        lastOk = true;
        lastError = null;
        commandsHandled++;
        try {
            switch (cmd) {
                case "open":
                    doOpen(intent);
                    break;
                case "close":
                    doClose();
                    break;
                case "play":
                    doPlay(intent);
                    break;
                case "record":
                    doRecord(intent);
                    break;
                case "status":
                    break;
                default:
                    fail("unknown command");
            }
        } catch (RuntimeException | IOException e) {
            fail(e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        writeStatus();
    }

    private void fail(String message) {
        lastOk = false;
        lastError = message;
    }

    // --- route ---------------------------------------------------------------------------------

    private void doOpen(Intent intent) {
        String name = intent.getStringExtra("device");
        wantedDevice = name == null ? DEFAULT_DEVICE : name;
        List<BluetoothDevice> connected = headset.getConnectedDevices();
        if (connected.size() != 1 || !wantedDevice.equals(connected.get(0).getName())) {
            fail("need " + wantedDevice + " as the only connected headset, connected: " + connected.size());
            return;
        }
        routeTarget = connected.get(0);
        AudioDeviceInfo target = findSco();
        if (target == null) {
            fail("no SCO route");
            routeTarget = null;
            return;
        }
        openLatencyMs = -1;
        openRequestedAt = SystemClock.elapsedRealtime();
        routeLosses = 0;
        opened = true;
        AudioAttributes attrs = voiceAttributes();
        focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
                .setAudioAttributes(attrs).build();
        am.requestAudioFocus(focus);
        am.setMode(AudioManager.MODE_IN_COMMUNICATION);
        // The mode is dropped by the system a few seconds later unless the app has active audio.
        if (output == null) {
            output = new OutputLoop(attrs);
            output.start();
        }
        if (!am.setCommunicationDevice(target)) {
            fail("setCommunicationDevice refused");
        }
        updateRouteProgress();
    }

    private void doClose() {
        if (recorder != null) {
            recorder.cancel();
        }
        if (output != null) {
            output.cancel();
            output = null;
        }
        am.clearCommunicationDevice();
        am.setMode(AudioManager.MODE_NORMAL);
        if (focus != null) {
            am.abandonAudioFocusRequest(focus);
            focus = null;
        }
        opened = false;
        routeTarget = null;
    }

    private AudioDeviceInfo findSco() {
        for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
            if (isScoRoute(d)) {
                return d;
            }
        }
        return null;
    }

    private static boolean isScoRoute(AudioDeviceInfo d) {
        return d != null && d.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO;
    }

    /** Confirmed only when the audio policy, the SCO broadcast and the headset profile all agree. */
    private boolean routeConfirmed() {
        AudioDeviceInfo now = am.getCommunicationDevice();
        return opened && routeTarget != null && isScoRoute(now)
                && "CONNECTED".equals(scoState) && headset != null && headset.isAudioConnected(routeTarget);
    }

    private void updateRouteProgress() {
        if (opened && openLatencyMs < 0 && routeConfirmed()) {
            openLatencyMs = SystemClock.elapsedRealtime() - openRequestedAt;
        }
    }

    private static String scoStateName(int state) {
        switch (state) {
            case AudioManager.SCO_AUDIO_STATE_CONNECTED:
                return "CONNECTED";
            case AudioManager.SCO_AUDIO_STATE_CONNECTING:
                return "CONNECTING";
            case AudioManager.SCO_AUDIO_STATE_DISCONNECTED:
                return "DISCONNECTED";
            default:
                return "ERROR";
        }
    }

    private static AudioAttributes voiceAttributes() {
        return new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                .build();
    }

    // --- playback ------------------------------------------------------------------------------

    private void doPlay(Intent intent) throws IOException {
        if (output == null) {
            fail("route is not open");
            return;
        }
        String wav = intent.getStringExtra("wav");
        short[] samples;
        JSONObject info = new JSONObject();
        int rate = TRACK_RATE;
        if (wav != null) {
            WavFile file = WavFile.read(new File(filesDir, new File(wav).getName()));
            samples = file.samples;
            rate = file.rate;
            put(info, "wav", wav);
        } else {
            String tone = intent.getStringExtra("tone");
            if (tone == null) {
                fail("play needs tone or wav");
                return;
            }
            int seconds = clampSeconds(intent.getIntExtra("seconds", 5));
            int level = intent.getIntExtra("level", DEFAULT_LEVEL);
            samples = makeTones(tone, seconds, level);
            put(info, "tone", tone);
            put(info, "level", level);
        }
        put(info, "rate", rate);
        put(info, "samples", samples.length);
        put(info, "state", "playing");
        put(info, "id", lastId);
        lastPlay = info;
        output.play(samples, rate);
    }

    private static short[] makeTones(String list, int seconds, int level) {
        String[] parts = list.split(",");
        int[] freqs = new int[parts.length];
        for (int i = 0; i < parts.length; i++) {
            freqs[i] = Integer.parseInt(parts[i].trim());
        }
        int count = TRACK_RATE * seconds;
        short[] out = new short[count];
        for (int n = 0; n < count; n++) {
            double sum = 0;
            for (int f : freqs) {
                sum += Math.sin(2 * Math.PI * f * n / TRACK_RATE);
            }
            out[n] = (short) (level * sum / freqs.length);
        }
        return out;
    }

    private void onPlayDone(int written) {
        if (lastPlay != null) {
            put(lastPlay, "state", "done");
            put(lastPlay, "written", written);
        }
        writeStatus();
    }

    /**
     * One voice-communication track for the life of the route. It streams silence between `play`
     * commands: a silent active track is what keeps MODE_IN_COMMUNICATION alive.
     */
    private final class OutputLoop extends Thread {
        private final AudioAttributes attrs;
        private volatile boolean stop = false;
        private short[] pending;
        private int pendingRate;

        OutputLoop(AudioAttributes attrs) {
            super("sco-output");
            this.attrs = attrs;
        }

        void cancel() {
            stop = true;
        }

        synchronized void play(short[] samples, int rate) {
            pending = samples;
            pendingRate = rate;
        }

        private synchronized short[] takePending() {
            short[] s = pending;
            pending = null;
            return s;
        }

        private AudioTrack newTrack(int rate) {
            int min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
            AudioTrack track = new AudioTrack.Builder()
                    .setAudioAttributes(attrs)
                    .setAudioFormat(new AudioFormat.Builder()
                            .setSampleRate(rate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build())
                    .setBufferSizeInBytes(Math.max(min, rate / 5 * 2))
                    .setTransferMode(AudioTrack.MODE_STREAM)
                    .build();
            track.play();
            return track;
        }

        @Override
        public void run() {
            int rate = TRACK_RATE;
            AudioTrack track = newTrack(rate);
            short[] silence = new short[rate / 50];
            try {
                while (!stop) {
                    short[] samples = takePending();
                    if (samples == null) {
                        track.write(silence, 0, silence.length);
                        continue;
                    }
                    if (pendingRate != rate) {
                        track.stop();
                        track.release();
                        rate = pendingRate;
                        track = newTrack(rate);
                        silence = new short[rate / 50];
                    }
                    int off = 0;
                    while (!stop && off < samples.length) {
                        int n = track.write(samples, off, Math.min(rate / 50, samples.length - off));
                        if (n <= 0) {
                            break;
                        }
                        off += n;
                    }
                    final int written = off;
                    main.post(new Runnable() {
                        @Override
                        public void run() {
                            onPlayDone(written);
                        }
                    });
                }
            } finally {
                track.pause();
                track.flush();
                track.release();
            }
        }
    }

    // --- recording -----------------------------------------------------------------------------

    private void doRecord(Intent intent) {
        if (checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            fail("RECORD_AUDIO not granted");
            return;
        }
        if (output == null || !routeConfirmed()) {
            fail("route is not open");
            return;
        }
        if (recorder != null) {
            fail("already recording");
            return;
        }
        int seconds = clampSeconds(intent.getIntExtra("seconds", 5));
        int rate = intent.getIntExtra("rate", RECORD_RATE);
        String out = intent.getStringExtra("out");
        String name = new File(out == null ? "record.wav" : out).getName();
        String source = intent.getStringExtra("source");
        source = source == null ? "vc" : source;
        boolean fx = intent.getIntExtra("fx", 1) != 0;
        JSONObject info = new JSONObject();
        put(info, "file", name);
        put(info, "seconds", seconds);
        put(info, "rate", rate);
        put(info, "source", source);
        put(info, "fx", fx);
        put(info, "state", "recording");
        put(info, "id", lastId);
        lastRecord = info;
        recorder = new RecordLoop(new File(filesDir, name), seconds, rate, am.getCommunicationDevice(), source, fx, info);
        recorder.start();
    }

    private void onRecordDone(int frames, int peak, double rms, String error) {
        recorder = null;
        if (lastRecord != null) {
            put(lastRecord, "state", error == null ? "done" : "failed");
            put(lastRecord, "frames", frames);
            put(lastRecord, "peak", peak);
            put(lastRecord, "rms", Math.round(rms * 10) / 10.0);
            if (error != null) {
                put(lastRecord, "error", error);
            }
        }
        writeStatus();
    }

    private final class RecordLoop extends Thread {
        private final File file;
        private final int seconds;
        private final int rate;
        private final AudioDeviceInfo device;
        private final int source;
        private final boolean fx;
        private final JSONObject info;
        private volatile boolean stop = false;

        RecordLoop(File file, int seconds, int rate, AudioDeviceInfo device, String source, boolean fx, JSONObject info) {
            super("sco-record");
            this.file = file;
            this.seconds = seconds;
            this.rate = rate;
            this.device = device;
            this.source = audioSource(source);
            this.fx = fx;
            this.info = info;
        }

        void cancel() {
            stop = true;
        }

        @Override
        public void run() {
            int frames = 0;
            int peak = 0;
            double energy = 0;
            String error = null;
            AudioRecord rec = null;
            int total = seconds * rate;
            try (RandomAccessFile out = new RandomAccessFile(file, "rw")) {
                out.setLength(0);
                out.write(WavFile.header(0, rate));
                int min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
                rec = new AudioRecord.Builder()
                        .setAudioSource(source)
                        .setAudioFormat(new AudioFormat.Builder()
                                .setSampleRate(rate)
                                .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .build())
                        .setBufferSizeInBytes(Math.max(min, rate / 2 * 2))
                        .build();
                rec.setPreferredDevice(device);
                configureEffects(rec.getAudioSessionId(), fx, info);
                rec.startRecording();
                short[] buf = new short[rate / 50];
                ByteBuffer bytes = ByteBuffer.allocate(buf.length * 2).order(ByteOrder.LITTLE_ENDIAN);
                while (!stop && frames < total) {
                    int n = rec.read(buf, 0, Math.min(buf.length, total - frames));
                    if (n <= 0) {
                        error = "read returned " + n;
                        break;
                    }
                    bytes.clear();
                    for (int i = 0; i < n; i++) {
                        bytes.putShort(buf[i]);
                        int abs = Math.abs((int) buf[i]);
                        peak = Math.max(peak, abs);
                        energy += (double) buf[i] * buf[i];
                    }
                    out.write(bytes.array(), 0, n * 2);
                    frames += n;
                }
                out.seek(0);
                out.write(WavFile.header(frames, rate));
            } catch (IOException | RuntimeException e) {
                error = e.getClass().getSimpleName() + ": " + e.getMessage();
            } finally {
                if (rec != null) {
                    rec.release();
                }
            }
            final int f = frames;
            final int p = peak;
            final double r = frames > 0 ? Math.sqrt(energy / frames) : 0;
            final String err = error;
            main.post(new Runnable() {
                @Override
                public void run() {
                    onRecordDone(f, p, r, err);
                }
            });
        }
    }

    private static int audioSource(String name) {
        switch (name) {
            case "mic":
                return MediaRecorder.AudioSource.MIC;
            case "recog":
                return MediaRecorder.AudioSource.VOICE_RECOGNITION;
            case "raw":
                return MediaRecorder.AudioSource.UNPROCESSED;
            default:
                return MediaRecorder.AudioSource.VOICE_COMMUNICATION;
        }
    }

    /** Reports which platform effects exist on this recording and switches them when asked. */
    private static void configureEffects(int session, boolean enable, JSONObject info) {
        JSONObject state = new JSONObject();
        AudioEffect[] effects = {
                AcousticEchoCanceler.isAvailable() ? AcousticEchoCanceler.create(session) : null,
                NoiseSuppressor.isAvailable() ? NoiseSuppressor.create(session) : null,
                AutomaticGainControl.isAvailable() ? AutomaticGainControl.create(session) : null,
        };
        String[] names = {"aec", "ns", "agc"};
        for (int i = 0; i < effects.length; i++) {
            if (effects[i] == null) {
                put(state, names[i], "none");
                continue;
            }
            boolean before = effects[i].getEnabled();
            if (!enable) {
                effects[i].setEnabled(false);
            }
            put(state, names[i], before + "->" + effects[i].getEnabled());
        }
        put(info, "effects", state);
    }

    // --- status --------------------------------------------------------------------------------

    private static int clampSeconds(int seconds) {
        return Math.max(1, Math.min(seconds, MAX_SECONDS));
    }

    private static void put(JSONObject o, String key, Object value) {
        try {
            o.put(key, value == null ? JSONObject.NULL : value);
        } catch (JSONException e) {
            throw new IllegalStateException(e);
        }
    }

    private static JSONObject describe(AudioDeviceInfo d) {
        JSONObject o = new JSONObject();
        put(o, "type", d.getType());
        put(o, "sco", d.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO);
        put(o, "name", String.valueOf(d.getProductName()));
        return o;
    }

    private void writeStatus() {
        JSONObject s = new JSONObject();
        AudioDeviceInfo route = am.getCommunicationDevice();
        put(s, "cmd", lastCmd);
        put(s, "id", lastId);
        put(s, "ok", lastOk);
        put(s, "error", lastError);
        put(s, "commands", commandsHandled);
        put(s, "elapsed_ms", SystemClock.elapsedRealtime());
        put(s, "opened", opened);
        put(s, "route_sco", isScoRoute(route));
        put(s, "route_confirmed", routeConfirmed());
        put(s, "route", route == null ? null : describe(route));
        put(s, "headset_audio", headset != null && routeTarget != null && headset.isAudioConnected(routeTarget));
        put(s, "mode", am.getMode());
        put(s, "sco_state", scoState);
        put(s, "sco_state_changes", scoStateChanges);
        put(s, "route_losses", routeLosses);
        put(s, "open_latency_ms", openLatencyMs);
        put(s, "wanted_device", wantedDevice);
        put(s, "record_permission",
                checkSelfPermission(android.Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED);
        put(s, "play", lastPlay);
        put(s, "record", lastRecord);
        JSONArray available = new JSONArray();
        for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
            available.put(describe(d));
        }
        put(s, "available", available);
        String text;
        try {
            text = s.toString(1);
        } catch (JSONException e) {
            text = s.toString();
        }
        Log.i(TAG, s.toString());
        view.setText(text);
        // Written next to the target and renamed, so that a reader never sees half a file.
        File tmp = new File(filesDir, "status.json.tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(text.getBytes("UTF-8"));
        } catch (IOException e) {
            Log.e(TAG, "status write failed", e);
            return;
        }
        if (!tmp.renameTo(new File(filesDir, "status.json"))) {
            Log.e(TAG, "status rename failed");
        }
    }
}

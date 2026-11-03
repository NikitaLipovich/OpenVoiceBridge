package com.openvoicebridge.scoprobe;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;

/** 16 bit PCM mono WAV, the only format this tool reads and writes. */
final class WavFile {
    final short[] samples;
    final int rate;

    private WavFile(short[] samples, int rate) {
        this.samples = samples;
        this.rate = rate;
    }

    static byte[] header(int frames, int rate) {
        int dataBytes = frames * 2;
        ByteBuffer b = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        b.put("RIFF".getBytes()).putInt(36 + dataBytes).put("WAVE".getBytes());
        b.put("fmt ".getBytes()).putInt(16).putShort((short) 1).putShort((short) 1);
        b.putInt(rate).putInt(rate * 2).putShort((short) 2).putShort((short) 16);
        b.put("data".getBytes()).putInt(dataBytes);
        return b.array();
    }

    static WavFile read(File file) throws IOException {
        ByteBuffer b = ByteBuffer.wrap(Files.readAllBytes(file.toPath())).order(ByteOrder.LITTLE_ENDIAN);
        if (b.remaining() < 12 || b.getInt(0) != 0x46464952 || b.getInt(8) != 0x45564157) {
            throw new IOException("not a WAV file");
        }
        int pos = 12;
        int rate = 0;
        while (pos + 8 <= b.limit()) {
            int id = b.getInt(pos);
            int size = b.getInt(pos + 4);
            int body = pos + 8;
            if (size < 0 || body + size > b.limit()) {
                size = b.limit() - body;
            }
            if (id == 0x20746d66) {            // "fmt "
                if (b.getShort(body) != 1 || b.getShort(body + 2) != 1 || b.getShort(body + 14) != 16) {
                    throw new IOException("need 16 bit PCM mono");
                }
                rate = b.getInt(body + 4);
            } else if (id == 0x61746164) {     // "data"
                if (rate == 0) {
                    throw new IOException("data before fmt");
                }
                short[] out = new short[size / 2];
                for (int i = 0; i < out.length; i++) {
                    out[i] = b.getShort(body + i * 2);
                }
                return new WavFile(out, rate);
            }
            pos = body + size + (size & 1);
        }
        throw new IOException("no data chunk");
    }
}

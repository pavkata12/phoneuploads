package bg.phoneuploads;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.net.Uri;
import android.os.Build;
import android.os.IBinder;
import android.os.ParcelFileDescriptor;
import android.os.PowerManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;

import bg.phoneuploads.net.ServerClient;
import bg.phoneuploads.net.TailscaleAddress;

public class UploadService extends Service {
    public static volatile boolean stopRequested;
    public static volatile String statusText = "Чака";

    private static final String CHANNEL = "uploads";
    private static final int NOTE_ID = 7;
    private PowerManager.WakeLock wakeLock;
    private Thread worker;

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationChannel channel = new NotificationChannel(CHANNEL, "Сървър на Ренди", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Показва докъде е стигнало видеото");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) {
            manager.createNotificationChannel(channel);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification notification = note("Сървър на Ренди", "Подготовка…", 0, 0);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTE_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTE_ID, notification);
        }
        if (worker == null || !worker.isAlive()) {
            stopRequested = false;
            worker = new Thread(this::uploadAll, "phoneuploads");
            worker.start();
        }
        return START_STICKY;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        // The upload keeps going after the screen is closed.
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void uploadAll() {
        PowerManager power = getSystemService(PowerManager.class);
        if (power != null) {
            wakeLock = power.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "PhoneUploads:upload");
            wakeLock.setReferenceCounted(false);
            wakeLock.acquire(12L * 60L * 60L * 1000L);
        }
        try {
            show("Сървър на Ренди", "Свързване…", -1, 0);
            SharedPreferences prefs = getSharedPreferences("phoneuploads", MODE_PRIVATE);
            JSONArray queue = readQueue();
            if (queue.length() == 0) {
                show("Няма файлове", "Избери видео и натисни Качи.", 0, 0);
                return;
            }
            ServerClient client = new ServerClient(TailscaleAddress.parse(prefs.getString("url", "")));
            show("Сървър на Ренди", "Влизане в сървъра на Ренди…", -1, 0);
            client.login(prefs.getString("password", ""));
            while (queue.length() > 0 && !stopRequested) {
                boolean finished = uploadOne(client, queue.getJSONObject(0));
                if (!finished) {
                    break;
                }
                queue.remove(0);
                writeQueue(queue);
            }
            if (stopRequested) {
                show("Спряно", "Отвори приложението и натисни Качи, за да продължи.", 0, 0);
            } else {
                show("Готово", "Файловете са на лаптопа.", 0, 0);
            }
        } catch (SocketTimeoutException ex) {
            show("Няма връзка", "Лаптопът не отговаря. Включи Tailscale и остави програмата на лаптопа отворена.", 0, 0);
        } catch (ConnectException ex) {
            show("Няма връзка", "Телефонът не стига до лаптопа. Включи Tailscale и на двете.", 0, 0);
        } catch (Exception ex) {
            String message = ex.getMessage() == null ? "Качването спря." : ex.getMessage();
            show("Качването спря", message, 0, 0);
        } finally {
            if (wakeLock != null && wakeLock.isHeld()) {
                wakeLock.release();
            }
            stopForeground(STOP_FOREGROUND_DETACH);
            stopSelf();
        }
    }

    private boolean uploadOne(ServerClient client, JSONObject item) throws Exception {
        Uri uri = Uri.parse(item.getString("uri"));
        String name = item.getString("name");
        long size = item.getLong("size");
        String key = item.getString("key");
        show(name, "Започва…", -1, 0);
        ServerClient.UploadStart start = client.startUpload(name, size, key);
        long offset = start.offset;
        int chunkSize = Math.max(256 * 1024, start.chunkSize);
        int percent = percentOf(offset, size);
        show(name, percent + "%", 100, percent);
        try (ParcelFileDescriptor descriptor = getContentResolver().openFileDescriptor(uri, "r")) {
            if (descriptor == null) {
                throw new IllegalStateException("Телефонът не отвори „" + name + "“.");
            }
            FileInputStream stream = new FileInputStream(descriptor.getFileDescriptor());
            FileChannel channel = stream.getChannel();
            channel.position(offset);
            while (offset < size && !stopRequested) {
                int want = (int) Math.min(chunkSize, size - offset);
                byte[] data = readChunk(channel, want);
                final long startOffset = offset;
                offset = sendWithRetry(client, start.uploadId, offset, data, want, written -> {
                    int sentPercent = percentOf(startOffset + written, size);
                    show(name, "Изпраща се " + sentPercent + "%", 100, sentPercent);
                });
                percent = percentOf(offset, size);
                show(name, percent + "% на лаптопа", 100, percent);
            }
        }
        if (stopRequested || offset < size) {
            return false;
        }
        client.complete(start.uploadId);
        show(name, "Записан на лаптопа", 100, 100);
        return true;
    }

    private byte[] readChunk(FileChannel channel, int want) throws IOException {
        ByteBuffer buffer = ByteBuffer.allocate(want);
        while (buffer.hasRemaining()) {
            int count = channel.read(buffer);
            if (count < 0) {
                break;
            }
            if (count == 0) {
                throw new IOException("Телефонът не успя да прочете файла.");
            }
        }
        if (buffer.position() != want) {
            throw new IOException("Файлът се промени по време на качване.");
        }
        return buffer.array();
    }

    private int percentOf(long done, long size) {
        if (size < 1) {
            return 0;
        }
        return (int) Math.min(100, (done * 100L) / size);
    }

    private long sendWithRetry(
            ServerClient client,
            String uploadId,
            long offset,
            byte[] data,
            int length,
            java.util.function.IntConsumer onWritten) throws Exception {
        Exception last = null;
        for (int attempt = 1; attempt <= 3; attempt++) {
            if (stopRequested) {
                return offset;
            }
            try {
                return client.putChunk(uploadId, offset, data, length, onWritten);
            } catch (Exception ex) {
                last = ex;
                if (attempt == 3) {
                    break;
                }
                show("Опит " + (attempt + 1), "Връзката прекъсна. Продължавам същото парче.", 0, 0);
                Thread.sleep(attempt * 1000L);
            }
        }
        throw last == null ? new IllegalStateException("Едно парче не се качи.") : last;
    }

    private void show(String title, String text, int max, int progress) {
        statusText = text;
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) {
            manager.notify(NOTE_ID, note(title, text, max, progress));
        }
    }

    private Notification note(String title, String text, int max, int progress) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pending = PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder builder = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setContentTitle(title)
                .setContentText(text)
                .setContentIntent(pending)
                .setOngoing(true)
                .setOnlyAlertOnce(true);
        if (max < 0) {
            builder.setProgress(0, 0, true);
        } else if (max > 0) {
            builder.setProgress(max, progress, false);
        }
        builder.setStyle(new Notification.BigTextStyle().bigText(text));
        return builder.build();
    }

    private JSONArray readQueue() throws Exception {
        java.io.File file = queueFile();
        if (!file.isFile()) {
            return new JSONArray();
        }
        byte[] bytes = new byte[(int) file.length()];
        try (FileInputStream input = new FileInputStream(file)) {
            int read = 0;
            while (read < bytes.length) {
                int count = input.read(bytes, read, bytes.length - read);
                if (count < 0) {
                    break;
                }
                read += count;
            }
        }
        return new JSONArray(new String(bytes, StandardCharsets.UTF_8));
    }

    private void writeQueue(JSONArray queue) throws Exception {
        byte[] bytes = queue.toString().getBytes(StandardCharsets.UTF_8);
        try (FileOutputStream output = new FileOutputStream(queueFile(), false)) {
            output.write(bytes);
        }
    }

    private java.io.File queueFile() {
        return new java.io.File(getFilesDir(), "queue.json");
    }
}

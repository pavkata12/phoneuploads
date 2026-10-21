package bg.phoneuploads;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.os.PowerManager;
import android.provider.Settings;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.DocumentsContract;
import android.provider.OpenableColumns;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.FileOutputStream;
import java.net.ConnectException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;

import bg.phoneuploads.net.ServerClient;
import bg.phoneuploads.net.TailscaleAddress;

public class MainActivity extends Activity {
    private static final int PICK = 1;
    private static final int NOTES = 2;

    private EditText address;
    private EditText password;
    private TextView status;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable refresher = new Runnable() {
        @Override
        public void run() {
            status.setText(UploadService.statusText);
            handler.postDelayed(this, 1000);
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        address = findViewById(R.id.address);
        password = findViewById(R.id.password);
        status = findViewById(R.id.status);
        SharedPreferences prefs = prefs();
        address.setText(prefs.getString("url", ""));
        password.setText(prefs.getString("password", ""));

        Button save = findViewById(R.id.save);
        Button check = findViewById(R.id.check);
        Button pick = findViewById(R.id.pick);
        Button upload = findViewById(R.id.upload);
        Button stop = findViewById(R.id.stop);
        Button background = findViewById(R.id.background);
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                saveSettings();
            }
        });
        check.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                checkConnection();
            }
        });
        pick.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                if (!saveSettings()) {
                    return;
                }
                Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                intent.addCategory(Intent.CATEGORY_OPENABLE);
                intent.setType("*/*");
                intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"video/*", "image/*"});
                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
                intent.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
                startActivityForResult(intent, PICK);
            }
        });
        upload.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                if (!saveSettings()) {
                    return;
                }
                if (!queueFile().isFile() || queueFile().length() < 3) {
                    status.setText("Първо избери снимки или видео.");
                    return;
                }
                beginUpload();
            }
        });
        stop.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                UploadService.stopRequested = true;
                status.setText("Спирам след текущото парче…");
            }
        });
        background.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View view) {
                allowBackground(true);
            }
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        handler.post(refresher);
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(refresher);
        super.onPause();
    }

    private void allowBackground(boolean alwaysAsk) {
        if (Build.VERSION.SDK_INT < 23) {
            return;
        }
        PowerManager power = (PowerManager) getSystemService(POWER_SERVICE);
        boolean allowed = power != null && power.isIgnoringBatteryOptimizations(getPackageName());
        if (allowed && !alwaysAsk) {
            return;
        }
        if (allowed) {
            status.setText("Фоновият мотор е включен. Качването продължава при заключен екран.");
            return;
        }
        Intent intent = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
        intent.setData(Uri.parse("package:" + getPackageName()));
        try {
            startActivity(intent);
            status.setText("Разреши „На Ренди“, за да не спира качването на заден план.");
        } catch (Exception ex) {
            status.setText("Отвори батерията на телефона и махни ограничението за На Ренди.");
        }
    }

    private boolean saveSettings() {
        String url;
        try {
            url = TailscaleAddress.parse(address.getText().toString()).toString();
        } catch (IllegalArgumentException ex) {
            status.setText(ex.getMessage());
            return false;
        }
        String secret = password.getText().toString();
        if (secret.isEmpty()) {
            status.setText("Напиши паролата от лаптопа.");
            return false;
        }
        prefs().edit().putString("url", url).putString("password", secret).apply();
        address.setText(url);
        status.setText("Записано. Tailscale трябва да е включен.");
        return true;
    }

    private void checkConnection() {
        if (!saveSettings()) {
            return;
        }
        final String url = address.getText().toString();
        final String secret = password.getText().toString();
        status.setText("Проверявам връзката с лаптопа…");
        new Thread(new Runnable() {
            @Override
            public void run() {
                final String message = probe(url, secret);
                handler.post(new Runnable() {
                    @Override
                    public void run() {
                        status.setText(message);
                    }
                });
            }
        }).start();
    }

    private String probe(String url, String secret) {
        try {
            ServerClient client = new ServerClient(TailscaleAddress.parse(url));
            client.login(secret);
            return "Връзката е наред. Телефонът влезе в лаптопа.";
        } catch (SocketTimeoutException ex) {
            return "Лаптопът не отговаря. Остави програмата отворена и включи Tailscale.";
        } catch (ConnectException ex) {
            return "Телефонът не стига до лаптопа. Адресът трябва да е от прозореца на лаптопа.";
        } catch (Exception ex) {
            if (ex.getMessage() == null || ex.getMessage().isEmpty()) {
                return "Няма връзка с лаптопа.";
            }
            return ex.getMessage();
        }
    }

    private void beginUpload() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTES);
            return;
        }
        UploadService.stopRequested = false;
        UploadService.statusText = "Свързване със сървъра на Ренди…";
        allowBackground(false);
        Intent intent = new Intent(this, UploadService.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == NOTES && grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
            beginUpload();
        } else if (requestCode == NOTES) {
            status.setText("Разреши известията. Там се вижда прогресът, докато екранът е заключен.");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK || resultCode != RESULT_OK || data == null) {
            return;
        }
        JSONArray queue = new JSONArray();
        ClipData clip = data.getClipData();
        try {
            if (clip != null) {
                for (int i = 0; i < clip.getItemCount(); i++) {
                    addUri(queue, clip.getItemAt(i).getUri());
                }
            } else if (data.getData() != null) {
                addUri(queue, data.getData());
            }
            byte[] bytes = queue.toString().getBytes(StandardCharsets.UTF_8);
            try (FileOutputStream output = new FileOutputStream(queueFile(), false)) {
                output.write(bytes);
            }
            status.setText(queue.length() == 1 ? "1 файл е готов за качване." : queue.length() + " файла са готови за качване.");
        } catch (Exception ex) {
            status.setText(ex.getMessage() == null ? "Файловете не се отвориха." : ex.getMessage());
        }
    }

    private void addUri(JSONArray queue, Uri uri) throws Exception {
        getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION);
        String name = "video.mp4";
        long size = -1;
        try (Cursor cursor = getContentResolver().query(uri, null, null, null, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                int nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                int sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE);
                if (nameIndex >= 0 && !cursor.isNull(nameIndex)) {
                    name = cursor.getString(nameIndex);
                }
                if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) {
                    size = cursor.getLong(sizeIndex);
                }
            }
        }
        if (size < 1) {
            throw new IllegalStateException("„" + name + "“ няма размер и не може да се качи.");
        }
        String key;
        try {
            key = DocumentsContract.getDocumentId(uri);
        } catch (Exception ex) {
            key = name;
        }
        key = (key + "|" + size);
        if (key.length() > 300) {
            key = key.substring(0, 300);
        }
        JSONObject item = new JSONObject();
        item.put("uri", uri.toString());
        item.put("name", name);
        item.put("size", size);
        item.put("key", key);
        queue.put(item);
    }

    private SharedPreferences prefs() {
        return getSharedPreferences("phoneuploads", MODE_PRIVATE);
    }

    private java.io.File queueFile() {
        return new java.io.File(getFilesDir(), "queue.json");
    }
}

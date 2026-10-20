package bg.phoneuploads.net;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Talks to the existing laptop server. No extra login API is required. */
public final class ServerClient {
    private static final Pattern CSRF_FORM = Pattern.compile("name=\"csrf_token\" value=\"([^\"]+)\"");
    private static final Pattern CSRF_BOOT = Pattern.compile("\"csrfToken\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern JSON_STRING = Pattern.compile("\"(upload_id|detail|name)\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"");
    private static final Pattern JSON_LONG = Pattern.compile("\"(offset|chunk_size)\"\\s*:\\s*(-?\\d+)");

    private final URI base;
    private final Map<String, String> cookies = new LinkedHashMap<String, String>();
    private String csrf = "";

    public ServerClient(URI base) {
        this.base = base;
    }

    public void login(String password) throws IOException {
        HttpResult page = request("GET", "/login", null, null, null);
        Matcher form = CSRF_FORM.matcher(page.body);
        if (!form.find()) {
            throw new IOException("Лаптопът не върна страница за вход.");
        }
        String formToken = form.group(1);
        String encoded = "csrf_token=" + url(formToken) + "&password=" + url(password);
        HttpResult posted = request(
                "POST",
                "/login",
                "application/x-www-form-urlencoded",
                encoded.getBytes(StandardCharsets.UTF_8),
                null);
        if (posted.status == 401) {
            throw new IOException("Грешна парола.");
        }
        if (posted.status == 429) {
            throw new IOException("Твърде много опити. Изчакай и пробвай пак.");
        }
        if (posted.status != 303 && posted.status != 302) {
            throw new IOException("Входът не успя.");
        }
        HttpResult home = request("GET", "/", null, null, null);
        if (home.status != 200) {
            throw new IOException("След входа страницата не се отвори.");
        }
        Matcher boot = CSRF_BOOT.matcher(home.body);
        if (!boot.find()) {
            throw new IOException("Липсва защитният код за качване.");
        }
        csrf = boot.group(1);
    }

    public UploadStart startUpload(String filename, long size, String clientKey) throws IOException {
        String json = "{\"filename\":" + quote(filename) + ",\"size\":" + size + ",\"client_key\":" + quote(clientKey) + "}";
        HttpResult result = request("POST", "/api/uploads", "application/json; charset=utf-8", json.getBytes(StandardCharsets.UTF_8), csrf);
        if (result.status != 200) {
            throw new IOException(errorText(result, "Качването не започна."));
        }
        String id = jsonString(result.body, "upload_id");
        if (id == null || id.isEmpty()) {
            throw new IOException("Лаптопът не върна номер на качването.");
        }
        return new UploadStart(id, jsonLong(result.body, "offset", 0), jsonLong(result.body, "chunk_size", 4 * 1024 * 1024));
    }

    public long putChunk(String uploadId, long offset, byte[] data, int length) throws IOException {
        HttpResult result = request(
                "PUT",
                "/api/uploads/" + uploadId + "/chunk?offset=" + offset,
                "application/octet-stream",
                data,
                length,
                csrf);
        if (result.status == 409) {
            return jsonLong(result.body, "offset", offset);
        }
        if (result.status != 200) {
            throw new IOException(errorText(result, "Едно парче не се качи."));
        }
        return jsonLong(result.body, "offset", offset + length);
    }

    public String complete(String uploadId) throws IOException {
        HttpResult result = request("POST", "/api/uploads/" + uploadId + "/complete", "application/json; charset=utf-8", "{}".getBytes(StandardCharsets.UTF_8), csrf);
        if (result.status != 200) {
            throw new IOException(errorText(result, "Файлът не се записа докрай."));
        }
        String name = jsonString(result.body, "name");
        return name == null ? "" : name;
    }

    private HttpResult request(String method, String path, String contentType, byte[] body, String csrfToken) throws IOException {
        int length = body == null ? 0 : body.length;
        return request(method, path, contentType, body, length, csrfToken);
    }

    private HttpResult request(String method, String path, String contentType, byte[] body, int length, String csrfToken) throws IOException {
        HttpURLConnection connection = (HttpURLConnection) base.resolve(path).toURL().openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(20000);
        connection.setReadTimeout(600000);
        connection.setRequestMethod(method);
        if (!cookies.isEmpty()) {
            connection.setRequestProperty("Cookie", cookieHeader());
        }
        if (csrfToken != null && !csrfToken.isEmpty()) {
            connection.setRequestProperty("X-CSRF-Token", csrfToken);
        }
        if (body != null) {
            connection.setDoOutput(true);
            connection.setRequestMethod(method);
            if (contentType != null) {
                connection.setRequestProperty("Content-Type", contentType);
            }
            connection.setFixedLengthStreamingMode(length);
            OutputStream out = connection.getOutputStream();
            out.write(body, 0, length);
            out.flush();
        }
        int status = connection.getResponseCode();
        for (Map.Entry<String, List<String>> entry : connection.getHeaderFields().entrySet()) {
            if (entry.getKey() != null && entry.getKey().equalsIgnoreCase("Set-Cookie")) {
                rememberCookies(entry.getValue());
            }
        }
        InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
        String text = readText(stream);
        connection.disconnect();
        return new HttpResult(status, text);
    }

    private void rememberCookies(List<String> headers) {
        if (headers == null) {
            return;
        }
        for (String header : headers) {
            int semi = header.indexOf(';');
            String pair = semi >= 0 ? header.substring(0, semi) : header;
            int eq = pair.indexOf('=');
            if (eq > 0) {
                cookies.put(pair.substring(0, eq).trim(), pair.substring(eq + 1).trim());
            }
        }
    }

    private String cookieHeader() {
        StringBuilder builder = new StringBuilder();
        for (Map.Entry<String, String> entry : cookies.entrySet()) {
            if (builder.length() > 0) {
                builder.append("; ");
            }
            builder.append(entry.getKey()).append('=').append(entry.getValue());
        }
        return builder.toString();
    }

    private static String readText(InputStream stream) throws IOException {
        if (stream == null) {
            return "";
        }
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int count;
        int total = 0;
        while ((count = stream.read(buffer)) >= 0 && total < 200000) {
            output.write(buffer, 0, count);
            total += count;
        }
        stream.close();
        return output.toString(StandardCharsets.UTF_8);
    }

    private static String errorText(HttpResult result, String fallback) {
        String detail = jsonString(result.body, "detail");
        if (detail != null && !detail.isEmpty()) {
            return detail;
        }
        return fallback;
    }

    static String jsonString(String json, String key) {
        Matcher matcher = JSON_STRING.matcher(json);
        while (matcher.find()) {
            if (key.equals(matcher.group(1))) {
                return matcher.group(2).replace("\\\"", "\"").replace("\\\\", "\\");
            }
        }
        return null;
    }

    static long jsonLong(String json, String key, long fallback) {
        Matcher matcher = JSON_LONG.matcher(json);
        while (matcher.find()) {
            if (key.equals(matcher.group(1))) {
                return Long.parseLong(matcher.group(2));
            }
        }
        return fallback;
    }

    private static String quote(String value) {
        StringBuilder builder = new StringBuilder(value.length() + 2);
        builder.append('"');
        for (int i = 0; i < value.length(); i++) {
            char ch = value.charAt(i);
            if (ch == '\\' || ch == '"') {
                builder.append('\\').append(ch);
            } else if (ch == '\n') {
                builder.append("\\n");
            } else if (ch >= 32) {
                builder.append(ch);
            }
        }
        builder.append('"');
        return builder.toString();
    }

    private static String url(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    public static final class UploadStart {
        public final String uploadId;
        public final long offset;
        public final int chunkSize;

        public UploadStart(String uploadId, long offset, long chunkSize) {
            this.uploadId = uploadId;
            this.offset = offset;
            this.chunkSize = chunkSize > Integer.MAX_VALUE ? Integer.MAX_VALUE : (int) chunkSize;
        }
    }

    private static final class HttpResult {
        final int status;
        final String body;

        HttpResult(int status, String body) {
            this.status = status;
            this.body = body;
        }
    }
}

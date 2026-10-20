package bg.phoneuploads.net;

import java.net.URI;

/** Accepts only the laptop address shown by the Windows app. */
public final class TailscaleAddress {
    private TailscaleAddress() {}

    public static URI parse(String raw) {
        if (raw == null) {
            throw new IllegalArgumentException("Липсва адресът от лаптопа.");
        }
        String text = raw.trim();
        while (text.endsWith("/")) {
            text = text.substring(0, text.length() - 1);
        }
        URI uri;
        try {
            uri = URI.create(text);
        } catch (IllegalArgumentException ex) {
            throw new IllegalArgumentException("Адресът не е валиден. Копирай го от лаптопа.");
        }
        if (!"http".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null) {
            throw new IllegalArgumentException("Адресът трябва да започва с http://100.");
        }
        if (uri.getUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null) {
            throw new IllegalArgumentException("Адресът трябва да е само http://100.x.x.x:8787");
        }
        if (!inTailnet(uri.getHost())) {
            throw new IllegalArgumentException("Адресът трябва да е Tailscale адресът на лаптопа, 100.x.x.x.");
        }
        int port = uri.getPort();
        if (port != -1 && (port < 1 || port > 65535)) {
            throw new IllegalArgumentException("Портът в адреса не е валиден.");
        }
        return uri;
    }

    static boolean inTailnet(String host) {
        String[] parts = host.split("\\.");
        if (parts.length != 4) {
            return false;
        }
        int[] octets = new int[4];
        for (int i = 0; i < 4; i++) {
            if (parts[i].isEmpty() || (parts[i].length() > 1 && parts[i].charAt(0) == '0')) {
                return false;
            }
            try {
                octets[i] = Integer.parseInt(parts[i]);
            } catch (NumberFormatException ex) {
                return false;
            }
            if (octets[i] < 0 || octets[i] > 255) {
                return false;
            }
        }
        return octets[0] == 100 && octets[1] >= 64 && octets[1] <= 127;
    }
}

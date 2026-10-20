package bg.phoneuploads.net;

import java.nio.file.Files;
import java.nio.file.Path;

/** Desktop check: logs in and uploads one file. Not part of the phone app. */
public final class SmokeMain {
    public static void main(String[] args) throws Exception {
        if (args.length != 3) {
            throw new IllegalArgumentException("usage: baseUrl password file");
        }
        byte[] data = Files.readAllBytes(Path.of(args[2]));
        ServerClient client = new ServerClient(java.net.URI.create(args[0]));
        client.login(args[1]);
        ServerClient.UploadStart start = client.startUpload("smoke.mp4", data.length, "smoke-key");
        int chunk = Math.max(1, Math.min(start.chunkSize, 256 * 1024));
        long offset = start.offset;
        while (offset < data.length) {
            int length = (int) Math.min(chunk, data.length - offset);
            byte[] piece = new byte[length];
            System.arraycopy(data, (int) offset, piece, 0, length);
            offset = client.putChunk(start.uploadId, offset, piece, length);
        }
        String saved = client.complete(start.uploadId);
        System.out.println("SAVED " + saved + " bytes " + data.length);
    }
}

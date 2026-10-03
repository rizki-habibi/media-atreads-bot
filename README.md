# Universal Media ShopeePay Barcode Detector

Bot untuk menerima **URL media publik** dari berbagai platform, mengambil gambar/video yang tersedia secara publik, mendeteksi QR/barcode, dan menandai hasil yang mengandung kata ShopeePay/Shopee bila memang terbaca.

Platform yang dapat dikenali dari URL: Instagram, TikTok, YouTube/Shorts, Facebook, X/Twitter, Telegram, dan web publik.

## Cara kerja

1. Kirim URL posting/media ke `POST /ingest-url`.
2. Bot membaca metadata publik seperti `og:image`, `twitter:image`, `og:video`, dan waktu publikasi bila tersedia.
3. Gambar dipindai dengan OpenCV QR detector + ZBar/pyzbar.
4. Video dipindai pada beberapa titik waktu untuk mencari QR/barcode.
5. Hasil disimpan sementara selama maksimal 15 menit sejak `published_at`.
6. Data dan media sementara otomatis dibersihkan setelah TTL.

Bot **tidak melewati login, CAPTCHA, akun privat, atau pembatasan platform**. Jika suatu platform tidak memberikan media publik melalui halaman URL, gunakan URL media publik/API resmi sebagai input.

## API

### POST /ingest-url — rekomendasi

Cukup kirim URL posting atau media publik:

```json
{
  "url": "https://www.example.com/post/123",
  "source": "web"
}
```

`source` dan `published_at` boleh dikosongkan. Bot akan mencoba mengenali sumber dan membaca metadata publik.

### POST /ingest

Untuk adapter/API resmi yang sudah mengetahui URL media dan waktu publish:

```json
{
  "source": "tiktok",
  "media_url": "https://example.com/image.jpg",
  "published_at": "2026-10-03T06:30:00+07:00",
  "post_id": "post-123"
}
```

### GET /items

Mengambil hasil deteksi yang masih aktif.

### GET /health

Memeriksa service, TTL, dan sumber URL yang dikenali.

## Jalankan lokal

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python -m app
```

Linux membutuhkan ZBar:

```bash
sudo apt-get install libzbar0
```

## Catatan ShopeePay

Detector hanya menyatakan apa yang benar-benar berhasil dibaca dari QR/barcode. Payload QR tidak dianggap sebagai bukti pembayaran. Verifikasi pembayaran harus tetap menggunakan mekanisme resmi penyedia pembayaran.

## Keamanan

- Gunakan HTTPS.
- Batasi `ALLOWED_MEDIA_HOSTS` bila deployment hanya membutuhkan domain tertentu.
- Jangan menaruh token/API key platform di repo.
- Jangan menganggap metadata publik tersedia untuk semua platform; beberapa platform menyajikan halaman dinamis atau membatasi akses otomatis.

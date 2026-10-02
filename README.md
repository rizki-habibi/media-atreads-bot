# media-atreads-bot

Bot untuk menerima media dari sumber sosial, mendeteksi QR/barcode pembayaran, mengumpulkan hasil selama maksimal 15 menit sejak waktu publish, lalu menghapus data/media yang sudah kedaluwarsa.

> Catatan: kode QR ShopeePay/QRIS dapat berupa QR, sedangkan ShopeePay juga mendukung payment barcode pada Customer Presented Mode. Detector mencoba QR terlebih dahulu dan barcode linear melalui pyzbar.

## Alur

1. Sumber media mengirim metadata `media_url` + `published_at` ke `POST /ingest`.
2. Worker mengambil gambar dan mendeteksi QR/barcode.
3. Hasil disimpan di SQLite.
4. Item tetap aktif sampai 15 menit sejak publish.
5. Cleanup worker berjalan berkala dan menghapus item, file sementara, serta hasil kadaluarsa.
6. Endpoint `GET /items` hanya mengembalikan item yang belum kadaluarsa.

## Jalankan lokal

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python -m app
```

Linux membutuhkan library ZBar untuk pyzbar, misalnya `sudo apt-get install libzbar0`.

Salin `.env.example` menjadi `.env`.

## API

### POST /ingest

```json
{
  "source": "threads",
  "media_url": "https://example.com/image.jpg",
  "published_at": "2026-10-03T06:30:00+07:00",
  "post_id": "post-123"
}
```

### GET /items

Mengambil QR/barcode aktif yang belum melewati TTL.

### GET /health

Status service dan jumlah item aktif.

## Keamanan

- Jangan log isi QR/barcode ke log publik.
- Gunakan HTTPS.
- Batasi domain media melalui `ALLOWED_MEDIA_HOSTS` bila bot dipasang di internet.
- Validasi timestamp publish agar media lama tidak diproses sebagai media baru.
- TTL 15 menit dihitung dari `published_at`, bukan dari waktu bot menerima request.

## Integrasi Threads

Core detector tidak bergantung pada platform. Adapter sumber media dapat memanggil `/ingest`. Dengan begitu kredensial API platform tetap berada di adapter dan tidak masuk ke detector.

ShopeePay mendokumentasikan QR/QRIS dan payment barcode pada mode pembayaran tertentu. Jangan menganggap payload hasil scan sebagai bukti pembayaran; verifikasi pembayaran tetap harus melalui mekanisme resmi penyedia pembayaran.

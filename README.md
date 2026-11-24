# airband-link

受信機の LineOut 音声を PC で切り分け、Tailscale 経由でミュゼ（Muse）の VM に送り、文字起こしして一緒に「聞く」ための仕組み。

```
[受信機] --LineOut/録音端子--> [Windows PC: sender] --Tailscale--> [VM: receiver] --> 文字起こしログ
```

- sender が音声を常時監視し、声が入った区間だけを WAV に切って送る（無音は捨てる）
- receiver が faster-whisper で文字起こしし、日別のトランスクリプトに追記する
- 交信が終わってから文字になるまで、目安 30 秒前後

## 必要なもの

- エアバンド受信機（LineOut / 録音端子つき）
- 3.5mm オーディオケーブル（PC にライン入力がなければ USB オーディオキャプチャ）
- Windows PC（常時起動）
- Tailscale（PC と VM が同じ tailnet に入っていること）
- 共有シークレット（任意の文字列を決めて両側に同じものを設定）

## セットアップ

### 1. Tailscale

PC と VM を同じアカウントでサインインし、VM の Tailscale IP（100.x.x.x）を確認する。

### 2. sender（Windows PC）

```powershell
cd sender
pip install -r requirements.txt
python capture_send.py --list-devices   # 入力デバイス番号を確認
$env:AIRBAND_TOKEN = "決めたシークレット"
python capture_send.py --device 3 --url http://100.x.x.x:8765/upload
```

コマンドラインで常駐する。止めるときは Ctrl+C。
しきい値（`--threshold`）は、受信機のスケルチが開いていないときのノイズより少し上に合わせる。

### 3. receiver（VM）

```bash
cd receiver
pip install -r requirements.txt
AIRBAND_TOKEN="決めたシークレット" python receive_transcribe.py
```

- 受信した音声: `receiver/data/inbox/YYYYMMDD/`
- 文字起こし: `receiver/data/transcripts/YYYY-MM-DD.txt`

## 注意

- 個人で楽しむ範囲でのみ使うこと。受信した交信の内容を公開・転載しない（電波法第59条）。
- 管制交信は英語が基本だが日本語も混ざるため、文字起こしは言語自動判定。コールサイン等の誤変換は起こりうる。
- ポートは Tailscale 内でのみ使う想定。インターネットに直接公開しない。

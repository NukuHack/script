#!/usr/bin/env python3
"""
YouTube Audio Streamer - Stream audio without account or cookies.
Usage: python music.py <youtube_url_or_playlist>
"""

import sys
import subprocess
import threading
import queue
import time
import json
import os
import signal
import tempfile
import glob

class YouTubeStreamer:
    def __init__(self):
        self.audio_queue = queue.Queue()
        self.should_stop = False
        self.total_videos = 0
        self.current_processes = []

    def get_video_urls(self, url):
        cmd = ['yt-dlp', '--flat-playlist', '--dump-json', '--geo-bypass', url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            video_urls = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        data = json.loads(line)
                        if 'url' in data:
                            video_urls.append(data['url'])
                        elif 'id' in data:
                            video_urls.append(f"https://youtube.com/watch?v={data['id']}")
                    except json.JSONDecodeError:
                        continue
            if not video_urls:
                if 'youtube.com/watch' in url or 'youtu.be' in url:
                    video_urls = [url]
                else:
                    print("Error: No valid videos found.")
                    sys.exit(1)
            return video_urls
        except Exception as e:
            print(f"Error fetching playlist: {e}")
            sys.exit(1)

    def get_video_title(self, video_url):
        cmd = ['yt-dlp', '--no-playlist', '--get-title', '--geo-bypass', video_url]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        return "Unknown Track"

    def download_audio(self, video_url, output_path):
        """Try to download with the 'web' client and include_sabr=False."""
        # Only one attempt – clear, no clutter
        yt_cmd = [
            'yt-dlp', '--no-playlist',
            '--format', 'bestaudio',
            '--output', output_path,
            '--geo-bypass',
            '--extractor-args', 'youtube:player_client=web,include_sabr=False',
            '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            video_url
        ]
        try:
            print("  Downloading audio (web client, no account)...")
            result = subprocess.run(yt_cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0:
                files = glob.glob(output_path + '.*')
                if files:
                    return True, files[0]
            # Print only the first error line
            err = result.stderr.strip()
            if err:
                for line in err.split('\n'):
                    if 'ERROR' in line:
                        print(f"    {line[:150]}")
                        break
            return False, None
        except subprocess.TimeoutExpired:
            print("    Download timed out.")
            return False, None
        except Exception as e:
            print(f"    Download error: {e}")
            return False, None

    def play_audio(self, audio_data):
        if audio_data is None or self.should_stop:
            return False
        idx = audio_data['index'] + 1
        total = audio_data['total']
        title = audio_data['title']
        video_url = audio_data['video_url']

        print(f"\n🎵 [{idx}/{total}] Now playing: {title}")
        print("Controls: Space=Pause, Up/Down=Volume, Q=Quit")
        print("-" * 50)

        with tempfile.TemporaryDirectory() as tmpdir:
            base_path = os.path.join(tmpdir, 'audio')
            success, result = self.download_audio(video_url, base_path)
            if not success:
                print("❌ Failed to download audio.")
                print("YouTube is blocking this request. Try using a VPN or try again later.")
                print("You may also try installing the latest version of yt-dlp and Node.js.")
                return False
            audio_file = result

            print("  Playing audio...")
            try:
                play_cmd = ['mpv', '--no-video', '--volume=100', audio_file]
                proc = subprocess.Popen(play_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                self.current_processes = [proc]
                stdout, stderr = proc.communicate()
                if proc.returncode != 0:
                    err = stderr.decode('utf-8', errors='ignore').strip()
                    if err:
                        print(f"⚠️  mpv error: {err[:200]}")
                    print("  Trying ffplay...")
                    play_cmd = ['ffplay', '-nodisp', '-autoexit', '-volume', '100', audio_file]
                    proc = subprocess.Popen(play_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.current_processes = [proc]
                    stdout, stderr = proc.communicate()
                    if proc.returncode != 0:
                        err = stderr.decode('utf-8', errors='ignore').strip()
                        if err:
                            print(f"⚠️  ffplay error: {err[:200]}")
                        return False
                return True
            except FileNotFoundError:
                print("  mpv not found, trying ffplay...")
                try:
                    play_cmd = ['ffplay', '-nodisp', '-autoexit', '-volume', '100', audio_file]
                    proc = subprocess.Popen(play_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    self.current_processes = [proc]
                    stdout, stderr = proc.communicate()
                    if proc.returncode != 0:
                        err = stderr.decode('utf-8', errors='ignore').strip()
                        if err:
                            print(f"⚠️  ffplay error: {err[:200]}")
                        return False
                    return True
                except:
                    print("❌ Neither mpv nor ffplay available.")
                    return False
            except Exception as e:
                print(f"⚠️  Playback error: {e}")
                return False

    def fetch_worker(self, video_urls):
        total = len(video_urls)
        for i, vurl in enumerate(video_urls):
            if self.should_stop:
                break
            print(f"⏳ Fetching info for track {i+1}/{total}...")
            title = self.get_video_title(vurl)
            self.audio_queue.put({
                'index': i,
                'title': title,
                'video_url': vurl,
                'total': total
            })
            time.sleep(0.2)

    def play_playlist(self, url):
        print("📡 Fetching video URLs...")
        video_urls = self.get_video_urls(url)
        self.total_videos = len(video_urls)
        print(f"✅ Found {self.total_videos} videos")
        print("Press Ctrl+C to stop\n" + "="*50)

        def signal_handler(sig, frame):
            print("\n🛑 Stopping...")
            self.should_stop = True
            for p in self.current_processes:
                try:
                    p.terminate()
                except:
                    pass
        signal.signal(signal.SIGINT, signal_handler)

        fetcher = threading.Thread(target=self.fetch_worker, args=(video_urls,), daemon=True)
        fetcher.start()

        print("⏳ Loading first track...")
        first = None
        for _ in range(30):
            if self.should_stop:
                return
            try:
                first = self.audio_queue.get(timeout=1)
                if first is not None:
                    break
            except queue.Empty:
                pass
        if not first:
            print("❌ No tracks loaded.")
            return

        played = 0
        while played < self.total_videos and not self.should_stop:
            if first is not None:
                audio = first
                first = None
            else:
                try:
                    audio = self.audio_queue.get(timeout=1)
                except queue.Empty:
                    if not fetcher.is_alive():
                        break
                    continue

            success = self.play_audio(audio)
            played += 1
            if not success:
                print(f"⚠️  Failed to play track {played}")

        self.should_stop = True
        fetcher.join(timeout=1)
        for p in self.current_processes:
            try:
                p.terminate()
            except:
                pass
        if played >= self.total_videos:
            print("\n🎉 Playlist finished!")

def main():
    if len(sys.argv) < 2:
        print("Usage: python music.py <youtube_url>")
        sys.exit(1)
    streamer = YouTubeStreamer()
    streamer.play_playlist(sys.argv[1])

if __name__ == "__main__":
    main()

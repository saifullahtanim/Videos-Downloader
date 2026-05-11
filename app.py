from flask import Flask, request, render_template, jsonify, send_file
import os
import tempfile
import threading
import requests
import json
import re
from datetime import datetime
import time
import yt_dlp
import instaloader
from werkzeug.utils import secure_filename
import zipfile
import shutil
import uuid
import imageio_ffmpeg
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-here-change-this'

# Create downloads directory if it doesn't exist
DOWNLOAD_DIR = os.path.join(os.getcwd(), 'downloads')
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)


class QuietYDLLogger:
    def debug(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


BUNDLED_FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

DOWNLOAD_JOBS = {}
DOWNLOAD_JOBS_LOCK = threading.Lock()
DOWNLOAD_CANCEL_FLAGS = {}
DOWNLOAD_CANCEL_LOCK = threading.Lock()


def create_download_job():
    job_id = str(uuid.uuid4())
    with DOWNLOAD_JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = {
            'started_at': time.time(),
            'status': 'queued',
            'progress': 0,
            'message': 'Queued',
            'platform': None,
            'result': None,
            'error': None,
            'title': None,
            'total_bytes': None,
            'downloaded_bytes': None,
            'speed': None,
            'eta': None,
            'last_speed': None,
            'last_eta': None,
        }
    return job_id


def update_download_job(job_id, **updates):
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            return
        job.update(updates)


def get_download_job(job_id):
    with DOWNLOAD_JOBS_LOCK:
        return DOWNLOAD_JOBS.get(job_id)


def update_download_job_cancel_state(job_id, cancelled):
    with DOWNLOAD_CANCEL_LOCK:
        if cancelled:
            DOWNLOAD_CANCEL_FLAGS[job_id] = True
        else:
            DOWNLOAD_CANCEL_FLAGS.pop(job_id, None)


def set_download_cancelled(job_id):
    with DOWNLOAD_CANCEL_LOCK:
        DOWNLOAD_CANCEL_FLAGS[job_id] = True


def is_download_cancelled(job_id):
    with DOWNLOAD_CANCEL_LOCK:
        return DOWNLOAD_CANCEL_FLAGS.get(job_id, False)

class UniversalDownloader:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def get_video_info(self, url, cookies=None, impersonate=None, extractor_args=None):
        """Get available resolutions for an extractable video URL."""
        cookiefile_path = None
        try:
            url = self.normalize_youtube_url(url)
            base_ydl_opts = {
                'quiet': True,
                'no_warnings': True,
                'noplaylist': True,
            }

            # If cookies text was provided, write to a temporary cookies file and point yt-dlp to it
            if cookies:
                try:
                    import tempfile
                    cf = tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.txt', prefix='yt_cookies_')
                    cf.write(cookies)
                    cf.flush()
                    cf.close()
                    cookiefile_path = cf.name
                    base_ydl_opts['cookiefile'] = cookiefile_path
                except Exception:
                    cookiefile_path = None
            
            # Add X.com specific options if it's a Twitter/X link
            if 'twitter.com' in url.lower() or 'x.com' in url.lower():
                base_ydl_opts['headers'] = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                }
                base_ydl_opts['socket_timeout'] = 30

            # Optional impersonate/extractor args
            if impersonate:
                try:
                    base_ydl_opts['impersonate'] = str(impersonate)
                except Exception:
                    pass

            if extractor_args and isinstance(extractor_args, dict):
                base_ydl_opts['extractor_args'] = extractor_args

            info = None
            last_error = None
            merged_formats = []
            youtube_client_variants = [None]
            if self.detect_platform(url) == 'youtube':
                youtube_client_variants = [
                    None,
                    {'youtube': {'player_client': ['android', 'web']}},
                    {'youtube': {'player_client': ['tv', 'web']}},
                    {'youtube': {'player_client': ['ios', 'web']}},
                ]

            for variant in youtube_client_variants:
                ydl_opts = dict(base_ydl_opts)
                if variant:
                    ydl_opts['extractor_args'] = variant
                try:
                    # apply optional impersonate/extractor_args if present in ydl_opts
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        current_info = ydl.extract_info(url, download=False)
                    if current_info:
                        if info is None:
                            info = current_info
                        current_formats = current_info.get('formats') or []
                        if current_formats:
                            merged_formats.extend(current_formats)
                except yt_dlp.utils.DownloadError as error:
                    last_error = error
                    error_text = str(error).lower()
                    if self.detect_platform(url) == 'youtube' and ('not available' in error_text or 'unplayable' in error_text or 'video unavailable' in error_text):
                        time.sleep(0.5)
                        continue
                    raise

            if info is None:
                raise last_error or RuntimeError('Unable to extract video info')

            formats = merged_formats or (info.get('formats', []) or [])
            resolutions = set()

            def add_resolution(value):
                if value is None:
                    return

                if isinstance(value, int):
                    resolutions.add(value)
                    return

                text = str(value)
                match = re.search(r'(\d{3,4})p', text, re.IGNORECASE)
                if match:
                    resolutions.add(int(match.group(1)))
                    return

                if text.isdigit():
                    resolutions.add(int(text))

            for fmt in formats:
                height = fmt.get('height')
                vcodec = fmt.get('vcodec')
                resolution = fmt.get('resolution')
                format_note = fmt.get('format_note')

                if vcodec and vcodec != 'none':
                    add_resolution(height)
                    add_resolution(resolution)
                    add_resolution(format_note)

            # Some extractors expose only a small set of direct heights in the formats list.
            # Fall back to any reported display height values before returning.
            if not resolutions:
                for fmt in formats:
                    add_resolution(fmt.get('width'))
                    add_resolution(fmt.get('format_note'))
                    add_resolution(fmt.get('resolution'))

            sorted_resolutions = sorted(resolutions, reverse=True)

            if self.detect_platform(url) == 'youtube' and len(sorted_resolutions) < 3:
                fallback_resolutions = [1080, 720, 480, 360, 240, 144]
                sorted_resolutions = sorted(set(sorted_resolutions) | set(fallback_resolutions), reverse=True)

            return {
                'status': 'success',
                'title': info.get('title', 'Unknown'),
                'uploader': info.get('uploader', 'Unknown'),
                'platform': self.detect_platform(url),
                'resolutions': sorted_resolutions,
                'thumbnail': info.get('thumbnail')
            }
        except yt_dlp.utils.DownloadError as e:
            error_str = str(e).lower()
            if self.detect_platform(url) == 'youtube':
                fallback = self.fetch_youtube_oembed(url)
                if fallback:
                    return fallback
            # Provide specific error messages
            if 'private' in error_str or 'protected' in error_str:
                return {'status': 'error', 'message': '🔒 This content is from a private account.'}
            elif 'not available' in error_str or 'not found' in error_str or '404' in error_str:
                return {'status': 'error', 'message': '❌ Video not available. It may be deleted, private, age-restricted, or region-blocked.'}
            elif 'age' in error_str or 'sign in' in error_str:
                return {'status': 'error', 'message': '🔞 Video is age-restricted or requires sign-in.'}
            else:
                return {'status': 'error', 'message': f'Preview error: {str(e)[:80]}'}
        except Exception as e:
            error_msg = str(e).lower()
            if self.detect_platform(url) == 'youtube':
                fallback = self.fetch_youtube_oembed(url)
                if fallback:
                    return fallback
            # Provide specific error messages
            if 'private' in error_msg or 'protected' in error_msg:
                return {'status': 'error', 'message': '🔒 This content is from a private account.'}
            elif 'not found' in error_msg or '404' in error_msg:
                return {'status': 'error', 'message': '❌ Content not found. The URL may be invalid or content may have been deleted.'}
            elif 'x.com' in url.lower() or 'twitter.com' in url.lower():
                return {'status': 'error', 'message': f'❌ X.com preview failed. The account may be private or protected.'}
            else:
                return {'status': 'error', 'message': f'Preview error: {str(e)[:80]}'}
        finally:
            # Clean up temporary cookies file if created
            if cookiefile_path and os.path.exists(cookiefile_path):
                try:
                    os.remove(cookiefile_path)
                except:
                    pass
    
    def detect_platform(self, url):
        """Detect the platform from URL"""
        url = url.lower()
        if 'youtube.com' in url or 'youtu.be' in url:
            return 'youtube'
        elif 'instagram.com' in url:
            return 'instagram'
        elif 'facebook.com' in url or 'fb.watch' in url:
            return 'facebook'
        elif 'twitter.com' in url or 'x.com' in url:
            return 'twitter'
        elif 'tiktok.com' in url:
            return 'tiktok'
        elif 'pinterest.com' in url:
            return 'pinterest'
        elif 'linkedin.com' in url:
            return 'linkedin'
        elif 'snapchat.com' in url:
            return 'snapchat'
        elif 'reddit.com' in url:
            return 'reddit'
        elif 'twitch.tv' in url:
            return 'twitch'
        else:
            return 'unknown'

    def normalize_youtube_url(self, url):
        """Return a canonical YouTube watch URL when a video id is present."""
        parsed = urlparse(url)
        host = (parsed.netloc or '').lower()
        path = parsed.path or ''
        query = parse_qs(parsed.query)

        if 'youtu.be' in host:
            video_id = path.strip('/').split('/')[0]
            if video_id:
                return f'https://www.youtube.com/watch?v={video_id}'
            return url

        if 'youtube.com' not in host:
            return url

        video_id = query.get('v', [None])[0]
        if video_id:
            return urlunparse((
                parsed.scheme or 'https',
                'www.youtube.com',
                '/watch',
                '',
                urlencode({'v': video_id}),
                ''
            ))

        return url

    def fetch_youtube_oembed(self, url):
        """Fetch lightweight YouTube metadata for preview fallbacks."""
        try:
            canonical_url = self.normalize_youtube_url(url)
            response = self.session.get(
                'https://www.youtube.com/oembed',
                params={
                    'url': canonical_url,
                    'format': 'json',
                },
                timeout=10,
            )
            response.raise_for_status()
            data = response.json()
            return {
                'status': 'success',
                'title': data.get('title', 'Unknown'),
                'uploader': data.get('author_name', 'YouTube'),
                'platform': 'youtube',
                'resolutions': [],
                'thumbnail': data.get('thumbnail_url')
            }
        except Exception:
            return None
    
    def create_safe_filename(self, filename, max_length=100):
        """Create a safe filename"""
        # Remove invalid characters
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = filename.strip()
        if len(filename) > max_length:
            filename = filename[:max_length]
        return filename

    def _extract_media_preview(self, info, requested_resolution='auto'):
        thumbnail = info.get('thumbnail')
        if not thumbnail:
            thumbnails = info.get('thumbnails') or []
            if thumbnails:
                thumbnail = thumbnails[-1].get('url')

        if requested_resolution and requested_resolution != 'auto':
            resolution = f'{requested_resolution}p'
        else:
            height = info.get('height')
            resolution = None
            if height:
                resolution = f'{height}p'
            elif info.get('resolution'):
                resolution = str(info.get('resolution'))
            elif info.get('format_note'):
                resolution = str(info.get('format_note'))

        return {
            'thumbnail': thumbnail,
            'resolution': resolution,
            'duration': info.get('duration'),
        }

    def _flatten_single_media_download(self, download_folder):
        """Move a single downloaded media file from a temp folder into DOWNLOAD_DIR."""
        primary_exts = {'.mp4', '.mkv', '.webm', '.mov', '.m4v', '.mp3', '.m4a', '.flv', '.avi', '.ts'}
        fallback_exts = primary_exts | {'.jpg', '.jpeg', '.png', '.gif'}
        primary_files = []
        fallback_files = []

        for root, dirs, files in os.walk(download_folder):
            for filename in files:
                file_path = os.path.join(root, filename)
                ext = os.path.splitext(filename)[1].lower()
                if ext in primary_exts:
                    primary_files.append(file_path)
                elif ext in fallback_exts:
                    fallback_files.append(file_path)

        media_files = primary_files or fallback_files
        if not media_files:
            return None

        source_path = max(media_files, key=lambda path: os.path.getsize(path) if os.path.isfile(path) else 0)
        base_name = os.path.basename(source_path)
        destination_path = os.path.join(DOWNLOAD_DIR, base_name)

        if os.path.exists(destination_path):
            name, ext = os.path.splitext(base_name)
            index = 1
            while True:
                candidate_name = f'{name}_{index}{ext}'
                destination_path = os.path.join(DOWNLOAD_DIR, candidate_name)
                if not os.path.exists(destination_path):
                    break
                index += 1

        shutil.move(source_path, destination_path)

        # Remove any empty nested folders left behind after moving the file.
        for root, dirs, files in os.walk(download_folder, topdown=False):
            try:
                if root != download_folder and os.path.isdir(root) and not os.listdir(root):
                    os.rmdir(root)
            except Exception:
                pass

        try:
            if os.path.isdir(download_folder) and not os.listdir(download_folder):
                os.rmdir(download_folder)
        except Exception:
            pass

        return destination_path

    def _raise_if_cancelled(self, job_id):
        if job_id and is_download_cancelled(job_id):
            raise RuntimeError('Download cancelled by user')
    
    def _build_ydl_opts(self, path, filename_template, resolution='auto', progress_hook=None, cookies=None, impersonate=None, extractor_args=None):
        ydl_opts = {
            'outtmpl': os.path.join(path, filename_template),
            'noplaylist': True,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'subtitleslangs': ['en'],
            'ignoreerrors': True,
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            'logger': QuietYDLLogger(),
            'ffmpeg_location': BUNDLED_FFMPEG_EXE,
        }

        # Add cookies if provided (for X.com authentication)
        if cookies:
            ydl_opts['cookiefile'] = cookies

        if progress_hook:
            ydl_opts['progress_hooks'] = [progress_hook]

        # Format selection with automatic quality fallback
        if resolution and resolution != 'auto':
            try:
                height = int(resolution)
                # Request video+audio merged at specified height.
                # yt-dlp automatically falls back to best available if exact height unavailable.
                # Format: bestvideo[height<=X]+bestaudio/best means:
                #   - Try bestvideo at height <= X merged with bestaudio
                #   - If that fails, fall back to /best (which downloads best combined format)
                # This ensures: 480p video downloads if 1080p not available, always gets audio
                ydl_opts['format'] = f'bestvideo[height<={height}]+bestaudio/best'
                ydl_opts['merge_output_format'] = 'mp4'
            except ValueError:
                # If resolution is not numeric, request merged best video+audio
                ydl_opts['format'] = 'bestvideo+bestaudio/best'
                ydl_opts['merge_output_format'] = 'mp4'
        else:
            # Auto: prefer the highest available quality with audio
            ydl_opts['format'] = 'bestvideo+bestaudio/best'
        
        # Prefer ffmpeg for merging and ensure location is set via imageio_ffmpeg
        ydl_opts['prefer_ffmpeg'] = True
        if BUNDLED_FFMPEG_EXE:
            ydl_opts['ffmpeg_location'] = BUNDLED_FFMPEG_EXE
            ydl_opts['merge_output_format'] = 'mp4'

        # Optional yt-dlp impersonation string (e.g., 'desktop', 'mobile', 'tv')
        if impersonate:
            try:
                ydl_opts['impersonate'] = str(impersonate)
            except Exception:
                pass

        # Optional extractor args mapping (dict) to pass directly to yt-dlp
        if extractor_args and isinstance(extractor_args, dict):
            ydl_opts['extractor_args'] = extractor_args

        return ydl_opts

    def download_youtube_content(self, url, path, resolution='auto', progress_hook=None, format_type='video', impersonate=None, extractor_args=None, job_id=None):
        """Download YouTube videos, shorts, playlists"""
        try:
            self._raise_if_cancelled(job_id)
            url = self.normalize_youtube_url(url)
            ydl_opts = self._build_ydl_opts(path, '%(title)s.%(ext)s', resolution=resolution, progress_hook=progress_hook, impersonate=impersonate, extractor_args=extractor_args)

            if format_type == 'audio':
                ydl_opts['format'] = 'bestaudio/best'
                ydl_opts['postprocessors'] = [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }]
                ydl_opts.pop('merge_output_format', None)
                ydl_opts['outtmpl'] = os.path.join(path, '%(title)s.%(ext)s')
            
            # Enable verbose mode for debugging
            ydl_opts['quiet'] = False
            ydl_opts['no_warnings'] = False
            ydl_opts['noprogress'] = False
            ydl_opts['logger'] = None
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self._raise_if_cancelled(job_id)
                info = ydl.extract_info(url, download=True)
                self._raise_if_cancelled(job_id)
                
                if 'entries' in info:  # Playlist
                    titles = [entry.get('title', 'Unknown') for entry in info['entries'] if entry]
                    return {
                        'status': 'success',
                        'message': f'Downloaded {len(titles)} videos from playlist',
                        'titles': titles[:5],  # Show first 5 titles
                        'type': 'playlist'
                    }
                else:  # Single video
                    return {
                        'status': 'success',
                        'message': 'YouTube content downloaded successfully!',
                        'title': info.get('title', 'Unknown'),
                        'uploader': info.get('uploader', 'Unknown'),
                        **self._extract_media_preview(info, requested_resolution=resolution),
                        'type': 'video'
                    }
        except yt_dlp.utils.DownloadError as e:
            error_str = str(e)
            if 'not available' in error_str.lower():
                return {'status': 'error', 'message': f'❌ Video not available: This video may be deleted, private, age-restricted, or not available in your region.'}
            elif 'sign in' in error_str.lower() or 'age' in error_str.lower():
                return {'status': 'error', 'message': f'❌ Age-restricted or requires sign-in. Try a different video.'}
            else:
                return {'status': 'error', 'message': f'YouTube error: {error_str[:100]}'}
        except Exception as e:
            error_str = str(e)
            if 'age' in error_str.lower():
                return {'status': 'error', 'message': f'❌ Video is age-restricted. Please try a different video.'}
            return {'status': 'error', 'message': f'YouTube error: {error_str[:100]}'}
    
    
    def download_instagram_content(self, url, path):
        """Download Instagram posts, reels, stories, IGTV - try yt-dlp first, fallback to instaloader"""
        try:
            def get_latest_media_file(search_dir):
                """Find the most recently created media file recursively."""
                media_extensions = {'.mp4', '.mkv', '.webm', '.mov', '.jpg', '.jpeg', '.png', '.gif'}
                try:
                    if not os.path.exists(search_dir):
                        return None
                    files = []
                    for root, dirs, filenames in os.walk(search_dir):
                        for f in filenames:
                            if os.path.splitext(f)[1].lower() in media_extensions:
                                full_path = os.path.join(root, f)
                                if os.path.isfile(full_path):
                                    files.append((full_path, os.path.getmtime(full_path)))
                    if not files:
                        return None
                    return max(files, key=lambda x: x[1])[0]
                except Exception:
                    return None
            
            def cleanup_all_non_media(search_dir):
                """Aggressively delete ALL non-media files recursively."""
                media_extensions = {'.mp4', '.mkv', '.webm', '.mov', '.jpg', '.jpeg', '.png', '.gif'}
                unwanted_extensions = {'.txt', '.json', '.html', '.info.json', '.webp'}
                try:
                    if not os.path.exists(search_dir):
                        return
                    for root, dirs, filenames in os.walk(search_dir, topdown=False):
                        for f in filenames:
                            ext = os.path.splitext(f)[1].lower()
                            # Delete if it's an unwanted type OR not a known media format
                            if ext in unwanted_extensions or (ext not in media_extensions and ext != ''):
                                file_path = os.path.join(root, f)
                                try:
                                    os.remove(file_path)
                                except Exception:
                                    pass
                        # Try to remove empty directories
                        try:
                            if not os.listdir(root) and root != search_dir:
                                os.rmdir(root)
                        except Exception:
                            pass
                except Exception:
                    pass
            
            # Try with yt-dlp first (more reliable for Instagram)
            try:
                ydl_opts = {
                    'outtmpl': os.path.join(path, '%(title)s.%(ext)s'),
                    'quiet': False,
                    'no_warnings': False,
                    'socket_timeout': 30,
                    'skip_unavailable_fragments': True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        # Clean up all non-media files after download
                        cleanup_all_non_media(path)
                        media_file = get_latest_media_file(path)
                        return {
                            'status': 'success',
                            'message': f'Instagram content downloaded successfully!',
                            'type': 'video' if info.get('ext') in ('mp4', 'mkv', 'webm') else 'image',
                            'file': media_file,
                            'title': info.get('title', 'Instagram content')
                        }
            except Exception as yt_dlp_error:
                # Fallback to instaloader if yt-dlp fails
                pass
            
            # Fallback: use instaloader
            loader = instaloader.Instaloader(
                dirname_pattern=path,
                filename_pattern='{profile}_{mediaid}_{date_utc}',
                download_videos=True,
                download_video_thumbnails=False,
                download_geotags=False,
                download_comments=False,
                save_metadata=False,
                compress_json=False,
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                max_connection_attempts=3,
            )
            
            # Handle different Instagram URL types
            if '/stories/' in url:
                username = self.extract_instagram_username(url)
                if username:
                    target_dir = os.path.join(path, username)
                    profile = instaloader.Profile.from_username(loader.context, username)
                    for story in loader.get_stories([profile.userid]):
                        for item in story.get_items():
                            loader.download_storyitem(item, target=username)
                    cleanup_all_non_media(path)
                    media_file = get_latest_media_file(path)
                    return {
                        'status': 'success',
                        'message': f'Instagram stories downloaded for {username}',
                        'type': 'stories',
                        'file': media_file
                    }
            elif '/reel/' in url or '/p/' in url or '/tv/' in url:
                shortcode = self.extract_instagram_shortcode(url)
                post = instaloader.Post.from_shortcode(loader.context, shortcode)
                loader.download_post(post, target=post.owner_username)
                
                content_type = 'reel' if post.is_video else 'post'
                if post.typename == 'GraphSidecar':
                    content_type = 'carousel'
                
                target_dir = os.path.join(path, post.owner_username)
                cleanup_all_non_media(path)
                media_file = get_latest_media_file(path)
                
                return {
                    'status': 'success',
                    'message': f'Instagram {content_type} downloaded successfully!',
                    'username': post.owner_username,
                    'caption': post.caption[:100] + '...' if post.caption and len(post.caption) > 100 else post.caption,
                    'type': content_type,
                    'file': media_file
                }
            else:
                username = self.extract_instagram_username(url)
                profile = instaloader.Profile.from_username(loader.context, username)
                target_dir = os.path.join(path, username)
                
                count = 0
                for post in profile.get_posts():
                    if count >= 10:
                        break
                    loader.download_post(post, target=username)
                    count += 1
                
                cleanup_all_non_media(path)
                media_file = get_latest_media_file(path)
                
                return {
                    'status': 'success',
                    'message': f'Downloaded {count} recent posts from {username}',
                    'type': 'profile',
                    'file': media_file
                }
                
        except Exception as e:
            error_msg = str(e).lower()
            if 'rate' in error_msg or '429' in error_msg:
                return {
                    'status': 'error',
                    'message': 'Instagram rate limit reached. Please try again later or wait a few minutes before retrying.'
                }
            elif 'login' in error_msg or 'not available' in error_msg or 'auth' in error_msg:
                return {
                    'status': 'error',
                    'message': 'Instagram content not available. This content may require authentication or may be private.'
                }
            else:
                return {'status': 'error', 'message': f'Instagram error: {str(e)}'}
    
    def download_tiktok_content(self, url, path, progress_hook=None, resolution='auto'):
        """Download TikTok videos"""
        try:
            ydl_opts = self._build_ydl_opts(path, 'TikTok_%(uploader)s_%(title)s.%(ext)s', resolution=resolution, progress_hook=progress_hook)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'TikTok video downloaded successfully!',
                    'title': info.get('title', 'TikTok Video'),
                    'uploader': info.get('uploader', 'Unknown'),
                    **self._extract_media_preview(info),
                    'type': 'video'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'TikTok error: {str(e)}'}
    
    def download_twitter_content(self, url, path, progress_hook=None, resolution='auto', cookies=None):
        """Download Twitter/X videos, images, threads"""
        try:
            ydl_opts = self._build_ydl_opts(path, 'Twitter_%(uploader)s_%(title)s.%(ext)s', resolution=resolution, progress_hook=progress_hook, cookies=cookies)
            
            # Add X.com specific headers and options for better compatibility
            ydl_opts['headers'] = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'en-US,en;q=0.9',
            }
            
            # Add socket timeout for X.com which can be slow
            ydl_opts['socket_timeout'] = 30
            
            # Add retries for X.com rate limiting
            ydl_opts['retries'] = {'max_retries': 5, 'backoff_factor': 0.5}
            
            # If cookies are provided, this tweet is likely protected - we need authentication
            if not cookies:
                ydl_opts['writeinfojson'] = True  # Try to get more metadata
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Twitter/X content downloaded successfully!',
                    'title': info.get('title', 'Twitter Content'),
                    'uploader': info.get('uploader', 'Unknown'),
                    **self._extract_media_preview(info),
                    'type': 'tweet'
                }
        except Exception as e:
            error_msg = str(e).lower()
            # Provide helpful error messages for X.com specific issues
            if 'no video' in error_msg or 'no media' in error_msg:
                return {'status': 'error', 'message': 'No video found in this tweet. It may be a text-only or image-only tweet. Try uploading your X.com cookies in settings for access to protected tweets.'}
            elif 'private' in error_msg or 'protected' in error_msg:
                return {'status': 'error', 'message': 'This tweet is from a private account. Upload your X.com cookies in settings to access it.'}
            elif 'not found' in error_msg or '404' in error_msg:
                return {'status': 'error', 'message': 'Tweet not found. The URL may be invalid or the tweet may have been deleted.'}
            elif 'rate limit' in error_msg or 'too many' in error_msg:
                return {'status': 'error', 'message': 'X.com rate limit exceeded. Please wait a moment and try again.'}
            elif 'authenticate' in error_msg or 'login' in error_msg:
                return {'status': 'error', 'message': 'X.com authentication required. Please upload your X.com cookies in settings to download this video.'}
            else:
                return {'status': 'error', 'message': f'X.com download failed: {str(e)}'}
    
    def download_facebook_content(self, url, path, progress_hook=None, resolution='auto'):
        """Download Facebook videos, posts"""
        try:
            ydl_opts = self._build_ydl_opts(path, 'Facebook_%(title)s.%(ext)s', resolution=resolution, progress_hook=progress_hook)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Facebook content downloaded successfully!',
                    'title': info.get('title', 'Facebook Content'),
                    **self._extract_media_preview(info),
                    'type': 'video'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Facebook error: {str(e)}'}
    
    def download_reddit_content(self, url, path, progress_hook=None, resolution='auto'):
        """Download Reddit videos, images, gifs"""
        try:
            ydl_opts = self._build_ydl_opts(path, 'Reddit_%(title)s.%(ext)s', resolution=resolution, progress_hook=progress_hook)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'status': 'success',
                    'message': 'Reddit content downloaded successfully!',
                    'title': info.get('title', 'Reddit Post'),
                    **self._extract_media_preview(info),
                    'type': 'post'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Reddit error: {str(e)}'}
    
    def download_generic_content(self, url, path, progress_hook=None, resolution='auto', job_id=None):
        """Download from any supported platform using yt-dlp"""
        try:
            self._raise_if_cancelled(job_id)
            ydl_opts = self._build_ydl_opts(path, '%(extractor)s_%(title)s.%(ext)s', resolution=resolution, progress_hook=progress_hook)
            
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                self._raise_if_cancelled(job_id)
                info = ydl.extract_info(url, download=True)
                self._raise_if_cancelled(job_id)
                return {
                    'status': 'success',
                    'message': 'Content downloaded successfully!',
                    'title': info.get('title', 'Unknown'),
                    'extractor': info.get('extractor', 'Unknown'),
                    **self._extract_media_preview(info),
                    'type': 'media'
                }
        except Exception as e:
            return {'status': 'error', 'message': f'Download error: {str(e)}'}
    
    def extract_instagram_shortcode(self, url):
        """Extract shortcode from Instagram URL"""
        patterns = [
            r'/p/([^/?]+)',
            r'/reel/([^/?]+)',
            r'/tv/([^/?]+)'
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        return None
    
    def extract_instagram_username(self, url):
        """Extract username from Instagram URL"""
        match = re.search(r'instagram\.com/([^/?]+)', url)
        if match:
            return match.group(1)
        return None
    
    def download_content(self, url, custom_path=None, resolution='auto', progress_hook=None, format_type='video', cookies=None, impersonate=None, extractor_args=None, job_id=None):
        """Main download function"""
        path = custom_path or DOWNLOAD_DIR
        platform = self.detect_platform(url)
        
        # Create timestamped folder for this download
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        download_folder = os.path.join(path, f"{platform}_{timestamp}")
        os.makedirs(download_folder, exist_ok=True)
        
        try:
            self._raise_if_cancelled(job_id)
            result = None
            if platform == 'youtube':
                # Forward impersonate and extractor_args to youtube-specific downloader
                result = self.download_youtube_content(url, download_folder, resolution=resolution, progress_hook=progress_hook, format_type=format_type, impersonate=impersonate, extractor_args=extractor_args, job_id=job_id)
            elif platform == 'instagram':
                result = self.download_instagram_content(url, download_folder)
            elif platform == 'tiktok':
                result = self.download_tiktok_content(url, download_folder, progress_hook=progress_hook, resolution=resolution)
            elif platform == 'twitter':
                result = self.download_twitter_content(url, download_folder, progress_hook=progress_hook, resolution=resolution, cookies=cookies)
            elif platform == 'facebook':
                result = self.download_facebook_content(url, download_folder, progress_hook=progress_hook, resolution=resolution)
            elif platform == 'reddit':
                result = self.download_reddit_content(url, download_folder, progress_hook=progress_hook, resolution=resolution)
            else:
                # Try generic download for other platforms
                result = self.download_generic_content(url, download_folder, progress_hook=progress_hook, resolution=resolution, job_id=job_id)

            # If the download resulted in a single media file, move it to DOWNLOAD_DIR so
            # video downloads do not stay wrapped in a timestamped folder.
            try:
                if result and result.get('status') == 'success':
                    saved_path = self._flatten_single_media_download(download_folder)
                    if saved_path:
                        result['saved_path'] = saved_path
                        try:
                            result['file_size'] = os.path.getsize(saved_path)
                        except Exception:
                            pass
                    elif result.get('saved_path'):
                        try:
                            result['file_size'] = os.path.getsize(result['saved_path'])
                        except Exception:
                            pass
            except Exception:
                pass

            return result

        except Exception as e:
            error_message = str(e)
            if 'cancelled by user' in error_message.lower():
                return {'status': 'cancelled', 'message': 'Download cancelled. Please paste the link again to start over.'}
            return {'status': 'error', 'message': f'Unexpected error: {error_message}'}

# Initialize downloader
downloader = UniversalDownloader()

@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def download():
    """Handle download requests"""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        cookies = data.get('cookies', None)
        resolution = data.get('resolution', 'auto')
        format_type = data.get('format_type', 'video')
        async_mode = data.get('async', True)
        cookies = data.get('cookies', None)  # Optional X.com cookies
        impersonate = data.get('impersonate', None)
        extractor_args = data.get('extractor_args', None)
        
        if not url:
            return jsonify({'status': 'error', 'message': 'URL is required'})
        
        # Detect platform automatically
        platform = downloader.detect_platform(url)

        if not async_mode:
            result = downloader.download_content(url, resolution=resolution, format_type=format_type, cookies=cookies, impersonate=impersonate, extractor_args=extractor_args)
            result['platform'] = platform
            saved_path = result.get('saved_path') or result.get('file')
            if saved_path and os.path.exists(saved_path):
                try:
                    file_size = os.path.getsize(saved_path)
                    result['file_size'] = file_size
                    result['total_bytes'] = file_size
                    result['downloaded_bytes'] = file_size
                except Exception:
                    pass
            return jsonify(result)

        job_id = create_download_job()
        update_download_job(job_id, status='running', progress=1, message='Starting download', platform=platform)

        def progress_hook(info):
            state = info.get('status')
            if state == 'downloading':
                total_bytes = info.get('total_bytes') or info.get('total_bytes_estimate')
                downloaded_bytes = info.get('downloaded_bytes') or 0
                percent = 0
                if total_bytes:
                    percent = int((downloaded_bytes / total_bytes) * 100)
                eta = info.get('eta')
                speed = info.get('speed')
                parts = []
                if speed:
                    parts.append(f"{round(speed / 1024 / 1024, 2)} MB/s")
                if eta is not None:
                    parts.append(f"ETA {eta}s")
                message = 'Downloading'
                if parts:
                    message = f"Downloading • {' • '.join(parts)}"
                update_download_job(
                    job_id,
                    progress=max(1, min(percent, 99)),
                    message=message,
                    total_bytes=total_bytes,
                    downloaded_bytes=downloaded_bytes,
                    speed=speed,
                    eta=eta,
                    last_speed=speed or get_download_job(job_id).get('last_speed'),
                    last_eta=eta or get_download_job(job_id).get('last_eta'),
                )
            elif state == 'finished':
                update_download_job(job_id, progress=100, message='Finalizing download')

        def run_download_job():
            try:
                result = downloader.download_content(url, resolution=resolution, progress_hook=progress_hook, format_type=format_type, cookies=cookies, impersonate=impersonate, extractor_args=extractor_args, job_id=job_id)
                result['platform'] = platform
                if result.get('status') == 'cancelled':
                    update_download_job(job_id, status='cancelled', progress=0, message=result.get('message', 'Download cancelled'), error=result.get('message'))
                    return
                job_state = get_download_job(job_id) or {}
                started_at = job_state.get('started_at') or time.time()
                result['elapsed_seconds'] = max(0, int(time.time() - started_at))
                if job_state.get('last_speed') is not None:
                    result['speed'] = job_state.get('last_speed')
                saved_path = result.get('saved_path') or result.get('file')
                if saved_path and os.path.exists(saved_path):
                    try:
                        file_size = os.path.getsize(saved_path)
                        result['file_size'] = file_size
                        result['total_bytes'] = file_size
                        result['downloaded_bytes'] = file_size
                    except Exception:
                        pass
                update_download_job(job_id, status='success', progress=100, message=result.get('message', 'Completed'), result=result, title=result.get('title'))
            except Exception as e:
                error_text = str(e)
                if 'cancelled by user' in error_text.lower():
                    update_download_job(job_id, status='cancelled', progress=0, message='Download cancelled. Please paste the link again to start over.', error='Download cancelled. Please paste the link again to start over.')
                else:
                    update_download_job(job_id, status='error', progress=0, message='Download failed', error=error_text)

        threading.Thread(target=run_download_job, daemon=True).start()
        return jsonify({'status': 'started', 'job_id': job_id, 'platform': platform})
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Server error: {str(e)}'})

@app.route('/cancel-download/<job_id>', methods=['POST'])
def cancel_download(job_id):
    """Cancel an in-progress download job."""
    try:
        job = get_download_job(job_id)
        if not job:
            return jsonify({'status': 'error', 'message': 'Job not found'}), 404

        update_download_job_cancel_state(job_id, True)
        update_download_job(job_id, status='cancelled', progress=0, message='Download cancelled. Please paste the link again to start over.', error='Download cancelled. Please paste the link again to start over.')
        return jsonify({'status': 'success', 'message': 'Download cancelled'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error cancelling download: {str(e)}'}), 500

@app.route('/download-status/<job_id>')
def download_status(job_id):
    job = get_download_job(job_id)
    if not job:
        return jsonify({'status': 'error', 'message': 'Job not found'}), 404
    job['job_id'] = job_id
    return jsonify(job)

@app.route('/bulk-download', methods=['POST'])
def bulk_download():
    """Handle bulk download requests - sequential downloads (one at a time)"""
    try:
        data = request.get_json()
        items = data.get('items') or []

        if not items:
            return jsonify({'status': 'error', 'message': 'URLs/items list is required'})

        jobs = []
        job_queue = []

        # Create background jobs for each item
        for raw in items:
            try:
                if isinstance(raw, str):
                    url = raw.strip()
                    opts = {}
                elif isinstance(raw, dict):
                    url = (raw.get('url') or '').strip()
                    opts = {
                        'resolution': raw.get('resolution', 'auto'),
                        'format_type': raw.get('format_type', 'video'),
                        'cookies': raw.get('cookies'),
                        'impersonate': raw.get('impersonate'),
                        'extractor_args': raw.get('extractor_args')
                    }
                else:
                    continue

                if not url:
                    continue

                platform = downloader.detect_platform(url)
                job_id = create_download_job()
                update_download_job(job_id, status='running', progress=1, message='Queued (waiting)', platform=platform, title=None)
                
                job_queue.append((job_id, url, opts, platform))
                jobs.append({
                    'job_id': job_id,
                    'url': url,
                    'platform': platform,
                    'title': (raw.get('title') or url) if isinstance(raw, dict) else url,
                    'thumbnail': raw.get('thumbnail') if isinstance(raw, dict) else None,
                    'format_type': opts.get('format_type', 'video'),
                    'resolution': opts.get('resolution', 'auto'),
                })

            except Exception:
                continue

        if not jobs:
            return jsonify({'status': 'error', 'message': 'No valid URLs to process'})

        # Start single worker thread that processes queue sequentially
        def process_queue():
            for job_id, url, opts, platform in job_queue:
                if is_download_cancelled(job_id):
                    update_download_job(job_id, status='cancelled', progress=0, message='Cancelled', error='Download cancelled')
                    continue

                def make_progress_hook(jid):
                    def progress_hook(info):
                        state = info.get('status')
                        if state == 'downloading':
                            total_bytes = info.get('total_bytes') or info.get('total_bytes_estimate')
                            downloaded_bytes = info.get('downloaded_bytes') or 0
                            percent = 0
                            if total_bytes:
                                try:
                                    percent = int((downloaded_bytes / total_bytes) * 100)
                                except Exception:
                                    percent = 0
                            eta = info.get('eta')
                            speed = info.get('speed')
                            update_download_job(
                                jid,
                                progress=max(1, min(percent, 99)),
                                message='Downloading',
                                total_bytes=total_bytes,
                                downloaded_bytes=downloaded_bytes,
                                speed=speed,
                                eta=eta,
                                last_speed=speed or get_download_job(jid).get('last_speed'),
                                last_eta=eta or get_download_job(jid).get('last_eta'),
                            )
                        elif state == 'finished':
                            update_download_job(jid, progress=100, message='Finalizing')
                    return progress_hook

                try:
                    result = downloader.download_content(url, resolution=opts.get('resolution', 'auto'), progress_hook=make_progress_hook(job_id), format_type=opts.get('format_type', 'video'), cookies=opts.get('cookies'), impersonate=opts.get('impersonate'), extractor_args=opts.get('extractor_args'), job_id=job_id)
                    result['platform'] = downloader.detect_platform(url)
                    started_at = get_download_job(job_id).get('started_at') or time.time()
                    result['elapsed_seconds'] = max(0, int(time.time() - started_at))
                    if get_download_job(job_id).get('last_speed') is not None:
                        result['speed'] = get_download_job(job_id).get('last_speed')
                    saved_path = result.get('saved_path') or result.get('file')
                    file_size = None
                    if saved_path:
                        try:
                            file_size = os.path.getsize(saved_path)
                        except Exception:
                            file_size = None
                    if file_size is not None:
                        result['file_size'] = file_size
                        result['total_bytes'] = file_size
                        result['downloaded_bytes'] = file_size
                    job_snapshot = get_download_job(job_id) or {}
                    total_bytes = job_snapshot.get('total_bytes') or file_size
                    downloaded_bytes = job_snapshot.get('downloaded_bytes') or file_size
                    update_download_job(
                        job_id,
                        status='success',
                        progress=100,
                        message=result.get('message', 'Completed'),
                        result=result,
                        title=result.get('title'),
                        total_bytes=total_bytes,
                        downloaded_bytes=downloaded_bytes,
                        speed=result.get('speed') or job_snapshot.get('last_speed'),
                        eta=0,
                        last_speed=result.get('speed') or job_snapshot.get('last_speed'),
                        last_eta=0,
                    )
                except Exception as e:
                    err = str(e)
                    if 'cancelled by user' in err.lower():
                        update_download_job(job_id, status='cancelled', progress=0, message='Download cancelled', error='Download cancelled')
                    else:
                        update_download_job(job_id, status='error', progress=0, message='Download failed', error=err)

        # Start the queue processor thread
        thread = threading.Thread(target=process_queue, daemon=True)
        thread.start()

        return jsonify({'status': 'started', 'message': f'Started {len(jobs)} downloads (sequential)', 'jobs': jobs})
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Bulk download error: {str(e)}'})

@app.route('/video-info', methods=['POST'])
def video_info():
    """Return metadata and available resolutions for YouTube videos."""
    try:
        data = request.get_json()
        url = data.get('url', '').strip()
        cookies = data.get('cookies', None)
        impersonate = data.get('impersonate', None)
        extractor_args = data.get('extractor_args', None)

        if not url:
            return jsonify({'status': 'error', 'message': 'URL is required'})

        result = downloader.get_video_info(url, cookies=cookies, impersonate=impersonate, extractor_args=extractor_args)
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Preview server error: {str(e)}'})

@app.route('/downloads')
def list_downloads():
    """List downloaded files and folders"""
    try:
        items = []
        if os.path.exists(DOWNLOAD_DIR):
            for item in os.listdir(DOWNLOAD_DIR):
                item_path = os.path.join(DOWNLOAD_DIR, item)
                if os.path.isfile(item_path):
                    items.append({
                        'name': item,
                        'type': 'file',
                        'size': os.path.getsize(item_path)
                    })
                elif os.path.isdir(item_path):
                    file_count = len([f for f in os.listdir(item_path) if os.path.isfile(os.path.join(item_path, f))])
                    items.append({
                        'name': item,
                        'type': 'folder',
                        'file_count': file_count
                    })
        
        return jsonify({'items': items})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/download-file/<path:filename>')
def download_file(filename):
    """Download a specific file"""
    try:
        safe_filename = secure_filename(filename)
        file_path = os.path.join(DOWNLOAD_DIR, safe_filename)
        
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True)
        else:
            return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/download-folder/<foldername>')
def download_folder(foldername):
    """Download a folder as ZIP"""
    try:
        safe_foldername = secure_filename(foldername)
        folder_path = os.path.join(DOWNLOAD_DIR, safe_foldername)
        
        if os.path.exists(folder_path) and os.path.isdir(folder_path):
            # Create a temporary ZIP file
            temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
            temp_zip.close()
            
            with zipfile.ZipFile(temp_zip.name, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(folder_path):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.relpath(file_path, folder_path)
                        zipf.write(file_path, arcname)
            
            return send_file(temp_zip.name, as_attachment=True, download_name=f'{safe_foldername}.zip')
        else:
            return jsonify({'error': 'Folder not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/serve-file', methods=['GET'])
def serve_file():
    """Serve downloaded media files for preview"""
    try:
        file_path = request.args.get('path', '')
        if not file_path:
            return jsonify({'error': 'File path not provided'}), 400
        
        # Security: ensure path is within downloads directory
        file_path = os.path.normpath(file_path)
        download_dir_normalized = os.path.normpath(DOWNLOAD_DIR)
        
        if not file_path.startswith(download_dir_normalized):
            return jsonify({'error': 'Access denied'}), 403
        
        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            return jsonify({'error': 'File not found'}), 404
        
        # Determine MIME type
        import mimetypes
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = 'application/octet-stream'
        
        return send_file(file_path, mimetype=mime_type)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/supported-platforms')
def supported_platforms():
    """List supported platforms"""
    platforms = {
        'video_platforms': [
            'YouTube (videos, shorts, playlists)',
            'TikTok',
            'Twitter/X',
            'Facebook',
            'Instagram (Reels, IGTV)',
            'Reddit',
            'Twitch',
            'Vimeo',
            'Dailymotion'
        ],
        'social_platforms': [
            'Instagram (Posts, Stories, Reels, IGTV)',
            'Twitter/X (Tweets, Threads)',
            'Facebook (Posts, Videos)',
            'Reddit (Posts, Images, Videos)',
            'LinkedIn (Posts)',
            'Pinterest (Pins)'
        ],
        'features': [
            'Auto-platform detection',
            'Bulk downloads',
            'Stories download',
            'Playlist support',
            'High quality downloads',
            'Metadata preservation',
            'Subtitle downloads'
        ]
    }
    return jsonify(platforms)

@app.route('/clear-downloads', methods=['POST'])
def clear_downloads():
    """Clear all downloaded files"""
    try:
        if os.path.exists(DOWNLOAD_DIR):
            shutil.rmtree(DOWNLOAD_DIR)
            os.makedirs(DOWNLOAD_DIR)
        return jsonify({'status': 'success', 'message': 'Downloads cleared successfully'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error clearing downloads: {str(e)}'})

@app.route('/delete-download', methods=['POST'])
def delete_download():
    """Delete individual download file or folder"""
    try:
        data = request.json
        item_name = data.get('name', '')
        
        if not item_name:
            return jsonify({'status': 'error', 'message': 'No item name provided'}), 400
        
        # Prevent directory traversal attacks
        if '..' in item_name or item_name.startswith('/'):
            return jsonify({'status': 'error', 'message': 'Invalid item name'}), 400
        
        item_path = os.path.join(DOWNLOAD_DIR, item_name)
        
        # Verify path is within downloads directory
        if not os.path.abspath(item_path).startswith(os.path.abspath(DOWNLOAD_DIR)):
            return jsonify({'status': 'error', 'message': 'Access denied'}), 403
        
        if os.path.isfile(item_path):
            os.remove(item_path)
            return jsonify({'status': 'success', 'message': f'File deleted successfully'})
        elif os.path.isdir(item_path):
            shutil.rmtree(item_path)
            return jsonify({'status': 'success', 'message': f'Folder deleted successfully'})
        else:
            return jsonify({'status': 'error', 'message': 'Item not found'}), 404
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error deleting item: {str(e)}'}), 500

@app.route('/open-downloads-folder', methods=['POST'])
def open_downloads_folder():
    """Open the downloads folder in file explorer"""
    try:
        import subprocess
        import platform
        
        # Normalize path
        folder_path = os.path.abspath(DOWNLOAD_DIR)
        
        # Verify folder exists
        if not os.path.exists(folder_path):
            return jsonify({'status': 'error', 'message': 'Downloads folder not found'}), 404
        
        # Open folder based on operating system
        system = platform.system()
        if system == 'Windows':
            os.startfile(folder_path)
        elif system == 'Darwin':  # macOS
            subprocess.Popen(['open', folder_path])
        else:  # Linux and others
            subprocess.Popen(['xdg-open', folder_path])
        
        return jsonify({'status': 'success', 'message': 'Downloads folder opened'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error opening folder: {str(e)}'}), 500

@app.route('/open-file-location', methods=['POST'])
def open_file_location():
    """Open the location of a specific downloaded file and select it when possible."""
    try:
        import subprocess
        import platform

        data = request.get_json() or {}
        file_path = os.path.abspath(data.get('path', ''))

        if not file_path:
            return jsonify({'status': 'error', 'message': 'File path not provided'}), 400

        download_root = os.path.abspath(DOWNLOAD_DIR)
        if not file_path.startswith(download_root):
            return jsonify({'status': 'error', 'message': 'Access denied'}), 403

        if not os.path.exists(file_path):
            return jsonify({'status': 'error', 'message': 'File not found'}), 404

        system = platform.system()
        if system == 'Windows':
            subprocess.Popen(['explorer', '/select,', file_path])
        elif system == 'Darwin':
            subprocess.Popen(['open', '-R', file_path])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(file_path)])

        return jsonify({'status': 'success', 'message': 'File location opened'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Error opening file location: {str(e)}'}), 500

if __name__ == '__main__':
    print("=" * 60)
    print("UNIVERSAL SOCIAL MEDIA DOWNLOADER")
    print("=" * 60)
    print("Starting server...")
    print("Supported platforms: YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, and more!")
    print("Features: Stories, Reels, Posts, Videos, Bulk downloads")
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("DEBUG", "False").lower() == "true"
    print(f"Server running on: http://0.0.0.0:{port}")
    print("=" * 60)
    app.run(debug=debug_mode, host='0.0.0.0', port=port)


    
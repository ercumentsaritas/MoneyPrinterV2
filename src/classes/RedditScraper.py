import os
import time
import random
import subprocess
from typing import List, Dict, Optional

import requests

from config import ROOT_DIR
from status import error, info

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def fetch_viral_posts(
    subreddit: str, limit: int = 25, time_filter: str = "day"
) -> List[Dict]:
    """
    Fetch viral posts from a given subreddit using Reddit's JSON API.
    """
    # Basic rate-limit safety
    time.sleep(random.uniform(1.0, 2.0))

    url = f"https://www.reddit.com/r/{subreddit}/top/.json"
    params = {"limit": limit, "t": time_filter}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
        response.raise_for_status()
    except Exception as e:
        error(f"Failed to fetch Reddit posts from r/{subreddit}: {e}")
        return []

    try:
        data = response.json()
        posts = data["data"]["children"]
    except Exception as e:
        error(f"Invalid JSON from Reddit for r/{subreddit}: {e}")
        return []

    results: List[Dict] = []
    for post in posts:
        p = post.get("data", {})

        # Filters
        if p.get("over_18"):
            continue
        if p.get("is_video"):
            continue
        if p.get("score", 0) < 500:
            continue
        if p.get("upvote_ratio", 0) < 0.80:
            continue

        image_url: Optional[str] = None
        url_val = p.get("url", "")
        if any(url_val.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".gif"]):
            image_url = url_val
        elif p.get("post_hint") == "image":
            image_url = url_val

        # Only keep posts with a reasonably descriptive title
        title = p.get("title", "").strip()
        if len(title.split()) < 2:
            continue

        results.append(
            {
                "id": p.get("id"),
                "title": title,
                "score": p.get("score", 0),
                "image_url": image_url,
                "subreddit": p.get("subreddit"),
                "permalink": p.get("permalink"),
            }
        )

    return results


def fetch_viral_video_posts(
    subreddit: str, limit: int = 25, time_filter: str = "day"
) -> List[Dict]:
    """
    Fetch viral VIDEO posts from a given subreddit using Reddit's JSON API.
    Only returns posts where is_video is True (hosted on v.redd.it).
    """
    time.sleep(random.uniform(1.0, 2.0))

    url = f"https://www.reddit.com/r/{subreddit}/top/.json"
    params = {"limit": limit, "t": time_filter}

    try:
        response = requests.get(url, headers=HEADERS, params=params, timeout=15)
        response.raise_for_status()
    except Exception as e:
        error(f"Failed to fetch Reddit video posts from r/{subreddit}: {e}")
        return []

    try:
        data = response.json()
        posts = data["data"]["children"]
    except Exception as e:
        error(f"Invalid JSON from Reddit for r/{subreddit}: {e}")
        return []

    results: List[Dict] = []
    for post in posts:
        p = post.get("data", {})

        if p.get("over_18"):
            continue
        if not p.get("is_video"):
            continue
        if p.get("score", 0) < 500:
            continue

        title = p.get("title", "").strip()
        if len(title.split()) < 2:
            continue

        # Extract video URL from Reddit's media structure
        reddit_video = (p.get("media") or {}).get("reddit_video", {})
        fallback_url = reddit_video.get("fallback_url")
        if not fallback_url:
            continue

        # Duration filter: skip very long videos (>60s)
        duration = reddit_video.get("duration", 0)
        if duration > 60:
            continue

        results.append(
            {
                "id": p.get("id"),
                "title": title,
                "score": p.get("score", 0),
                "video_url": fallback_url,
                "duration": duration,
                "subreddit": p.get("subreddit"),
                "permalink": p.get("permalink"),
            }
        )

    return results


def download_image(image_url: str, save_path: str) -> Optional[str]:
    """
    Download an image from the given URL to save_path.
    Returns the saved path or None on failure.
    """
    try:
        resp = requests.get(image_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        error(f"Failed to download image from Reddit: {e}")
        return None

    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(resp.content)
    except Exception as e:
        error(f"Failed to write image to disk: {e}")
        return None

    info(f"Downloaded Reddit image to: {save_path}")
    return save_path


def download_reddit_video(
    video_url: str, save_path: str, audio_temp_path: str
) -> Optional[str]:
    """
    Download a Reddit-hosted video (v.redd.it).
    Attempts to download audio stream separately and merge with ffmpeg.
    Returns the final video path or None on failure.
    """
    # 1. Download video stream
    try:
        resp = requests.get(video_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        error(f"Failed to download Reddit video: {e}")
        return None

    video_only_path = save_path + ".video_only.mp4"
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(video_only_path, "wb") as f:
            f.write(resp.content)
    except Exception as e:
        error(f"Failed to write video to disk: {e}")
        return None

    # 2. Try to download audio stream
    # Reddit audio URL pattern: replace DASH_xxx.mp4 with DASH_AUDIO_128.mp4
    base_url = video_url.rsplit("/", 1)[0]
    audio_urls = [
        f"{base_url}/DASH_AUDIO_128.mp4",
        f"{base_url}/DASH_audio.mp4",
        f"{base_url}/DASH_AUDIO_64.mp4",
    ]

    audio_downloaded = False
    for audio_url in audio_urls:
        try:
            resp_audio = requests.get(audio_url, headers=HEADERS, timeout=15)
            if resp_audio.status_code == 200:
                with open(audio_temp_path, "wb") as f:
                    f.write(resp_audio.content)
                audio_downloaded = True
                break
        except Exception:
            continue

    # 3. Merge with ffmpeg if audio exists, otherwise just rename
    if audio_downloaded:
        try:
            cmd = [
                "ffmpeg", "-y",
                "-i", video_only_path,
                "-i", audio_temp_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                save_path,
            ]
            subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            info(f"Merged Reddit video+audio to: {save_path}")
        except Exception as e:
            error(f"ffmpeg merge failed, using video without audio: {e}")
            os.replace(video_only_path, save_path)
    else:
        os.replace(video_only_path, save_path)
        info(f"Downloaded Reddit video (no audio) to: {save_path}")

    # Cleanup temp files
    for tmp in [video_only_path, audio_temp_path]:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass

    return save_path


def select_best_post(posts: List[Dict], used_ids: List[str]) -> Optional[Dict]:
    """
    Select the best unused post from the provided list.
    Preference is given to higher-score posts that have not been used before.
    """
    unused_posts = [p for p in posts if p.get("id") and p["id"] not in used_ids]
    if not unused_posts:
        return None

    # Sort by score descending and pick the top one
    unused_posts.sort(key=lambda p: p.get("score", 0), reverse=True)
    return unused_posts[0]


def get_temp_image_paths() -> Dict[str, str]:
    """
    Helper to get standard temp image/video paths inside .mp directory.
    """
    temp_dir = os.path.join(ROOT_DIR, ".mp")
    os.makedirs(temp_dir, exist_ok=True)
    return {
        "raw": os.path.join(temp_dir, "viral_img_temp.jpg"),
        "anon": os.path.join(temp_dir, "viral_img_anon.jpg"),
        "video": os.path.join(temp_dir, "viral_video_temp.mp4"),
        "video_audio": os.path.join(temp_dir, "viral_video_audio_temp.mp4"),
    }


import re
import sys
import time
import os
import json
import random
from typing import List, Optional
from datetime import datetime, timedelta

from utils import *
from cache import *
from config import *
from status import *
from llm_provider import generate_text
from termcolor import colored
from selenium_firefox import *
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from webdriver_manager.firefox import GeckoDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from deep_translator import GoogleTranslator
from classes.RedditScraper import (
    fetch_viral_posts,
    fetch_viral_video_posts,
    download_image,
    download_reddit_video,
    select_best_post,
    get_temp_image_paths,
)
from classes.ImageAnonymizer import anonymize_image
from constants import MEME_SUBREDDITS


class Twitter:
    """
    Class for the Bot, that grows a Twitter account.
    """

    def __init__(
        self, account_uuid: str, account_nickname: str, fp_profile_path: str, topic: str
    ) -> None:
        """
        Initializes the Twitter Bot.

        Args:
            account_uuid (str): The account UUID
            account_nickname (str): The account nickname
            fp_profile_path (str): The path to the Firefox profile

        Returns:
            None
        """
        self.account_uuid: str = account_uuid
        self.account_nickname: str = account_nickname
        self.fp_profile_path: str = fp_profile_path
        self.topic: str = topic

        # Initialize the Firefox profile
        self.options: Options = Options()

        # Set headless state of browser (headless not recommended for Twitter)
        if get_headless():
            warning("Headless mode is enabled. For safer Twitter automation, set headless=false in config.json.")
            self.options.add_argument("--headless")

        if not os.path.isdir(fp_profile_path):
            raise ValueError(
                f"Firefox profile path does not exist or is not a directory: {fp_profile_path}"
            )

        # Harden webdriver fingerprint a bit via Firefox prefs
        self.options.set_preference("dom.webdriver.enabled", False)
        self.options.set_preference("useAutomationExtension", False)

        # Set the profile path
        self.options.add_argument("-profile")
        self.options.add_argument(fp_profile_path)

        # Set the service
        self.service: Service = Service(GeckoDriverManager().install())

        # Configure Firefox Binary
        firefox_bin = get_firefox_binary()
        if firefox_bin:
            self.options.binary_location = firefox_bin

        # Initialize the browser
        self.browser: webdriver.Firefox = webdriver.Firefox(
            service=self.service, options=self.options
        )
        self.wait: WebDriverWait = WebDriverWait(self.browser, 30)

        # Translator for LLM fallback
        self._translator: Optional[GoogleTranslator] = None

    def _dismiss_popups(self) -> None:
        """
        Dismiss any overlay popups / info dialogs that Twitter/X may show
        (e.g. 'Görüntülemeler' views dialog, cookie banners, etc.).
        Tries several strategies; silently ignores if nothing is found.
        """
        from selenium.webdriver.common.keys import Keys
        bot = self.browser
        strategies = [
            # 1. "Gizle" / "Dismiss" / "Got it" / "Not now" buttons
            (By.XPATH, "//button[.//span[contains(text(),'Gizle')]]"),
            (By.XPATH, "//button[.//span[contains(text(),'Dismiss')]]"),
            (By.XPATH, "//button[.//span[contains(text(),'Got it')]]"),
            (By.XPATH, "//button[.//span[contains(text(),'Not now')]]"),
            (By.XPATH, "//button[.//span[contains(text(),'Şimdi değil')]]"),
            (By.XPATH, "//button[.//span[contains(text(),'Tamam')]]"),
            # 2. Generic close / X button inside layers
            (By.XPATH, "//div[@data-testid='sheetDialog']//button[@aria-label='Close']"),
            (By.XPATH, "//div[@data-testid='sheetDialog']//button[@aria-label='Kapat']"),
            (By.XPATH, "//div[contains(@class,'Layer')]//button[@aria-label='Close']"),
            (By.XPATH, "//div[contains(@class,'Layer')]//button[@aria-label='Kapat']"),
            (By.CSS_SELECTOR, "div[role='dialog'] button[aria-label='Close']"),
            (By.CSS_SELECTOR, "div[role='dialog'] button[aria-label='Kapat']"),
            # 3. Generic dialog dismiss button (any role=dialog button)
            (By.XPATH, "//div[@role='dialog']//button[.//span[contains(text(),'Gizle')]]"),
        ]
        for selector in strategies:
            try:
                btn = bot.find_element(*selector)
                if btn.is_displayed():
                    btn.click()
                    time.sleep(0.5)
                    return
            except Exception:
                continue
        # 4. Fallback: press Escape to close any modal
        try:
            bot.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            time.sleep(0.3)
        except Exception:
            pass

    def _human_pause(self, min_sec: float = 1.0, max_sec: float = 3.5) -> None:
        """
        Human-like pause between actions.
        """
        time.sleep(random.uniform(min_sec, max_sec))

    def _human_type(self, element, text: str) -> None:
        """
        Type text into an element with human-like delays.
        """
        for char in text:
            element.send_keys(char)
            delay = random.uniform(0.05, 0.25)
            if random.random() < 0.15:
                delay += random.uniform(0.3, 0.8)
            time.sleep(delay)

    def _upload_image(self, image_path: str) -> bool:
        """
        Upload an image to the Twitter compose screen.
        Returns True on success, False on failure.
        """
        from selenium.webdriver.common.by import By  # local import to avoid cycles
        from selenium.common.exceptions import TimeoutException

        try:
            file_input_selectors = [
                (By.CSS_SELECTOR, "input[type='file'][accept*='image']"),
                (By.CSS_SELECTOR, "input[type='file'][accept*='video']"),
                (By.XPATH, "//input[@type='file']"),
            ]

            file_input = None
            for selector in file_input_selectors:
                try:
                    file_input = WebDriverWait(self.browser, 10).until(
                        EC.presence_of_element_located(selector)
                    )
                    break
                except (TimeoutException, Exception):
                    continue

            if file_input is None:
                error("File input element bulunamadı.")
                return False

            self.browser.execute_script(
                "arguments[0].style.display = 'block';"
                "arguments[0].style.visibility = 'visible';",
                file_input,
            )

            file_input.send_keys(os.path.abspath(image_path))
            self._human_pause(2.0, 4.0)

            # Verify the image was actually attached by checking for thumbnail
            try:
                WebDriverWait(self.browser, 10).until(
                    EC.presence_of_element_located(
                        (By.CSS_SELECTOR, "div[data-testid='attachments'] img, div[data-testid='attachments'] video")
                    )
                )
            except TimeoutException:
                error("Görsel yüklendi ama attachment doğrulanamadı.")
                return False

            return True
        except Exception as e:
            error(f"Görsel yüklenemedi: {e}")
            return False

    def _get_used_reddit_ids(self) -> list:
        """
        Read used Reddit IDs for this account from the twitter cache.
        """
        if not os.path.exists(get_twitter_cache_path()):
            return []

        try:
            with open(get_twitter_cache_path(), "r") as file:
                parsed = json.load(file)
        except Exception:
            return []

        for account in parsed.get("accounts", []):
            if account.get("id") == self.account_uuid:
                return account.get("used_reddit_ids", [])

        return []

    def _mark_reddit_id_used(self, reddit_id: str) -> None:
        """
        Persist a Reddit post ID as used for this account.
        """
        if not os.path.exists(get_twitter_cache_path()):
            with open(get_twitter_cache_path(), "w") as file:
                json.dump({"accounts": []}, file, indent=4)

        with open(get_twitter_cache_path(), "r") as file:
            previous_json = json.load(file)

        accounts = previous_json.get("accounts", [])
        target_account = None
        for account in accounts:
            if account.get("id") == self.account_uuid:
                target_account = account
                break

        if target_account is None:
            target_account = {
                "id": self.account_uuid,
                "nickname": self.account_nickname,
                "firefox_profile": self.fp_profile_path,
                "topic": self.topic,
                "posts": [],
                "used_reddit_ids": [],
            }
            accounts.append(target_account)

        used_ids = target_account.get("used_reddit_ids", [])
        if reddit_id not in used_ids:
            used_ids.append(reddit_id)
        target_account["used_reddit_ids"] = used_ids

        previous_json["accounts"] = accounts
        with open(get_twitter_cache_path(), "w") as f:
            json.dump(previous_json, f, indent=4)

    def _get_recent_meme_posts(self, hours: int = 24) -> List[dict]:
        """
        Return meme posts (those with reddit_id) within the last `hours`.
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        recent: List[dict] = []
        for post in self.get_posts():
            if "reddit_id" not in post:
                continue
            try:
                dt = datetime.strptime(post["date"], "%m/%d/%Y, %H:%M:%S")
            except Exception:
                continue
            if dt >= cutoff:
                recent.append(post)
        return recent

    def _get_last_meme_post(self) -> Optional[dict]:
        """
        Return the last meme post for this account, if any.
        """
        meme_posts = [p for p in self.get_posts() if "reddit_id" in p]
        if not meme_posts:
            return None
        # posts are appended chronologically, so last is newest
        return meme_posts[-1]

    def _enforce_shadowban_rules(self) -> bool:
        """
        Enforce basic shadowban safety rules for meme posting.
        Returns True if posting is allowed, False otherwise.
        """
        recent_24h = self._get_recent_meme_posts(24)
        if len(recent_24h) >= 3:
            warning("Shadowban guard: 24 saat içinde 3'ten fazla viral meme paylaşımı engellendi.")
            return False

        last_meme = self._get_last_meme_post()
        if last_meme:
            try:
                last_dt = datetime.strptime(last_meme["date"], "%m/%d/%Y, %H:%M:%S")
                if datetime.now() - last_dt < timedelta(hours=4):
                    warning("Shadowban guard: Son viral meme üzerinden 4 saatten az geçmiş, paylaşım atlandı.")
                    return False
            except Exception:
                pass

        return True

    def _should_use_image_this_time(self) -> bool:
        """
        Try to keep ~60% image, 40% text-only ratio historically.
        """
        posts = self.get_posts()
        meme_posts = [p for p in posts if "reddit_id" in p]
        if not meme_posts:
            return True

        with_image = [p for p in meme_posts if p.get("has_image")]
        ratio = len(with_image) / len(meme_posts)

        # If images are already >=0.6, prefer text-only; if below, prefer image.
        return ratio < 0.6

    def _translate_title_tr(self, title: str) -> str:
        """
        Basic fallback: translate Reddit title to Turkish using deep-translator.
        """
        try:
            result = GoogleTranslator(source="en", target="tr").translate(title)
            return result or title
        except Exception as e:
            warning(f"Translation fallback failed: {e}")
            return title

    def post(self, text: Optional[str] = None) -> None:
        """
        Starts the Twitter Bot.

        Args:
            text (str): The text to post

        Returns:
            None
        """
        bot: webdriver.Firefox = self.browser
        verbose: bool = get_verbose()

        bot.get("https://x.com/compose/post")
        self._human_pause(2.0, 4.0)
        self._dismiss_popups()

        post_content: str = text if text is not None else self.generate_post()
        now: datetime = datetime.now()

        print(colored(" => Posting to Twitter:", "blue"), post_content[:30] + "...")
        body = post_content

        text_box = None
        text_box_selectors = [
            (By.CSS_SELECTOR, "div[data-testid='tweetTextarea_0'][role='textbox']"),
            (By.XPATH, "//div[@data-testid='tweetTextarea_0']//div[@role='textbox']"),
            (By.XPATH, "//div[@role='textbox']"),
        ]

        for selector in text_box_selectors:
            try:
                text_box = self.wait.until(EC.element_to_be_clickable(selector))
                text_box.click()
                self._human_pause(0.5, 1.5)
                self._human_type(text_box, body)
                break
            except Exception:
                continue

        if text_box is None:
            raise RuntimeError(
                "Could not find tweet text box. Ensure you are logged into X in this Firefox profile."
            )


        post_button = None
        post_button_selectors = [
            (By.XPATH, "//button[@data-testid='tweetButtonInline']"),
            (By.XPATH, "//button[@data-testid='tweetButton']"),
            (By.XPATH, "//span[text()='Post']/ancestor::button"),
        ]

        for selector in post_button_selectors:
            try:
                post_button = self.wait.until(EC.element_to_be_clickable(selector))
                self._human_pause(1.0, 2.5)
                post_button.click()
                break
            except Exception:
                continue

        if post_button is None:
            raise RuntimeError("Could not find the Post button on X compose screen.")

        if verbose:
            print(colored(" => Pressed [ENTER] Button on Twitter..", "blue"))
        self._human_pause(2.0, 4.0)

        # Add the post to the cache
        self.add_post({"content": body, "date": now.strftime("%m/%d/%Y, %H:%M:%S")})

        success("Posted to Twitter successfully!")

    def get_posts(self) -> List[dict]:
        """
        Gets the posts from the cache.

        Returns:
            posts (List[dict]): The posts
        """
        if not os.path.exists(get_twitter_cache_path()):
            # Create the cache file
            with open(get_twitter_cache_path(), "w") as file:
                json.dump({"accounts": []}, file, indent=4)

        with open(get_twitter_cache_path(), "r") as file:
            parsed = json.load(file)

            # Find our account
            accounts = parsed.get("accounts", [])
            for account in accounts:
                if account.get("id") == self.account_uuid:
                    posts = account.get("posts", [])
                    return posts if posts is not None else []

        return []

    def add_post(self, post: dict) -> None:
        """
        Adds a post to the cache.

        Args:
            post (dict): The post to add

        Returns:
            None
        """
        if not os.path.exists(get_twitter_cache_path()):
            with open(get_twitter_cache_path(), "w") as file:
                json.dump({"accounts": []}, file, indent=4)

        with open(get_twitter_cache_path(), "r") as file:
            previous_json = json.load(file)

        accounts = previous_json.get("accounts", [])
        target_account = None
        for account in accounts:
            if account.get("id") == self.account_uuid:
                target_account = account
                break

        # Create account entry if it doesn't exist yet
        if target_account is None:
            target_account = {
                "id": self.account_uuid,
                "nickname": self.account_nickname,
                "firefox_profile": self.fp_profile_path,
                "topic": self.topic,
                "posts": [],
                "used_reddit_ids": [],
            }
            accounts.append(target_account)

        target_account.setdefault("posts", []).append(post)

        previous_json["accounts"] = accounts
        with open(get_twitter_cache_path(), "w") as f:
            json.dump(previous_json, f, indent=4)

    def generate_post(self) -> str:
        """
        Generates a post for the Twitter account based on the topic.

        Returns:
            post (str): The post
        """
        completion = generate_text(
            f"Generate a Twitter post about: {self.topic} in {get_twitter_language()}. "
            "The limit is 2 sentences. Choose a specific sub-topic of the provided topic. DO NOT use emojis."
        )

        if get_verbose():
            info("Generating a post...")

        if completion is None:
            error("Failed to generate a post. Please try again.")
            sys.exit(1)

        # Apply Regex to remove all *
        completion = re.sub(r"\*", "", completion).replace('"', "")

        if get_verbose():
            info(f"Length of post: {len(completion)}")
        if len(completion) >= 260:
            return completion[:257].rsplit(" ", 1)[0] + "..."

        return completion

    def _build_localization_prompt(self, reddit_title: str, subreddit: str) -> str:
        return (
            "Sen Türkiye'nin en başarılı mizah Twitter hesabının sosyal medya uzmanısın. "
            "Z kuşağı Türk internet argosu ve Twitter dinamiklerini çok iyi biliyorsun.\n\n"
            "Aşağıdaki Reddit postunu (r/"
            f"{subreddit}"
            ") Türk Twitter kullanıcıları için yerelleştir. "
            "Kelimesi kelimesine çeviri yapma. Espriyi veya mesajı al, Türk kültürüne, "
            "gündemine ve internet argosuyla uyumlu, özgün ve viral olabilecek bir tweet yaz.\n\n"
            f"Reddit Başlığı: \"{reddit_title}\"\n\n"
            "Kurallar:\n"
            "- Maksimum 250 karakter\n"
            "- KESİNLİKLE EMOJİ KULLANMA\n"
            "- Hashtag KULLANMA\n"
            "- Sadece tweet metnini yaz, başka hiçbir şey ekleme\n"
            "- Alıntı işareti ve yıldız (*) kullanma\n"
            "- Noktalama işaretlerini minimum düzeyde kullan. Twitter'da insanlar cümle sonu nokta koymaz virgülü de nadiren kullanır"
        )

    def fetch_and_post_viral_meme(self) -> None:
        """
        Full pipeline: Reddit → LLM localization → video/image → Twitter post.
        Tries video posts first, falls back to image posts.
        """
        verbose = get_verbose()

        # 0. Shadowban protection rules (quota + spacing)
        if not self._enforce_shadowban_rules():
            return

        # 1. Choose subreddit and try video posts first, then image posts
        subreddit = random.choice(MEME_SUBREDDITS)
        if verbose:
            info(f"Fetching viral posts from r/{subreddit}...")

        # Try video posts first
        video_posts = fetch_viral_video_posts(subreddit, limit=25, time_filter="day")
        if not video_posts:
            if verbose:
                warning("No video posts found for 'day', trying 'week'...")
            video_posts = fetch_viral_video_posts(subreddit, limit=25, time_filter="week")

        used_ids = self._get_used_reddit_ids()
        selected = None
        is_video_post = False
        image_posts = []

        if video_posts:
            selected = select_best_post(video_posts, used_ids)
            if selected:
                is_video_post = True

        # Fall back to image posts if no video
        if not selected:
            image_posts = fetch_viral_posts(subreddit, limit=25, time_filter="day")
            if not image_posts:
                if verbose:
                    warning("No image posts found for 'day', trying 'week'...")
                image_posts = fetch_viral_posts(subreddit, limit=25, time_filter="week")
            if image_posts:
                selected = select_best_post(image_posts, used_ids)

        if not selected:
            error("No suitable Reddit posts found.")
            return

        # 2.b Simple subreddit rotation
        last_meme = self._get_last_meme_post()

        reddit_id = selected["id"]
        reddit_title = selected["title"]
        subreddit_name = selected["subreddit"]

        if last_meme and last_meme.get("reddit_subreddit") == subreddit_name:
            all_posts = (video_posts or []) + (image_posts or [])
            alt = [p for p in all_posts if p.get("subreddit") != subreddit_name and p.get("id") not in used_ids]
            if alt:
                alt_selected = select_best_post(alt, used_ids)
                if alt_selected:
                    selected = alt_selected
                    reddit_id = selected["id"]
                    reddit_title = selected["title"]
                    subreddit_name = selected["subreddit"]
                    is_video_post = "video_url" in selected

        if verbose:
            media_type = "video" if is_video_post else "image/text"
            info(f"Selected Reddit post ({media_type}): [{subreddit_name}] {reddit_title[:80]}...")

        # 3. LLM localization
        prompt = self._build_localization_prompt(reddit_title, subreddit_name)
        completion = generate_text(prompt)

        if not completion:
            warning("LLM failed to generate localized tweet. Retrying once...")
            time.sleep(30)
            completion = generate_text(prompt)

        if not completion:
            warning("LLM still failed. Falling back to translation of Reddit title.")
            body = self._translate_title_tr(reddit_title)
        else:
            body = re.sub(r"\*", "", completion).replace('"', "").strip()
            if len(body) >= 260:
                body = body[:257].rsplit(" ", 1)[0] + "..."

        # 4/5. Media download (video or image)
        temp_paths = get_temp_image_paths()
        media_to_upload = None

        if is_video_post:
            video_url = selected.get("video_url")
            if video_url:
                if verbose:
                    info(f"Downloading video from Reddit: {video_url}")
                video_path = download_reddit_video(
                    video_url, temp_paths["video"], temp_paths["video_audio"]
                )
                if video_path:
                    media_to_upload = video_path
                    if verbose:
                        info("Reddit video downloaded successfully.")
                else:
                    warning("Video download failed. Proceeding with text-only tweet.")
        else:
            image_url = selected.get("image_url")
            want_image = image_url and self._should_use_image_this_time()

            if want_image:
                if verbose:
                    info(f"Downloading image from Reddit: {image_url}")
                url_lower = image_url.lower()
                if url_lower.endswith(".png"):
                    ext = ".png"
                elif url_lower.endswith(".gif"):
                    ext = ".gif"
                else:
                    ext = ".jpg"
                raw_save_path = os.path.splitext(temp_paths["raw"])[0] + ext
                anon_save_path = os.path.splitext(temp_paths["anon"])[0] + ext

                raw_path = download_image(image_url, raw_save_path)
                if raw_path:
                    if ext == ".gif":
                        media_to_upload = raw_path
                        if verbose:
                            info("GIF detected — skipping anonymization to preserve animation.")
                    else:
                        try:
                            anon_path = anonymize_image(raw_path, anon_save_path)
                            media_to_upload = anon_path
                            if verbose:
                                info("Image anonymization succeeded.")
                        except Exception as e:
                            warning(f"Image anonymization failed, using raw image: {e}")
                            media_to_upload = raw_path
                else:
                    warning("Image download failed. Proceeding with text-only tweet.")

        # 6–12. Compose and post on Twitter with human-like behavior
        try:
            bot: webdriver.Firefox = self.browser
            bot.get("https://x.com/compose/post")
            self._human_pause(2.0, 4.0)
            self._dismiss_popups()

            body_text = body
            now: datetime = datetime.now()

            print(colored(" => Posting localized Reddit meme to Twitter:", "blue"), body_text[:60] + "...")

            text_box = None
            text_box_selectors = [
                (By.CSS_SELECTOR, "div[data-testid='tweetTextarea_0'][role='textbox']"),
                (By.XPATH, "//div[@data-testid='tweetTextarea_0']//div[@role='textbox']"),
                (By.XPATH, "//div[@role='textbox']"),
            ]

            for selector in text_box_selectors:
                try:
                    text_box = self.wait.until(EC.element_to_be_clickable(selector))
                    text_box.click()
                    self._human_pause(0.5, 1.5)
                    self._human_type(text_box, body_text)
                    break
                except Exception:
                    continue

            if text_box is None:
                raise RuntimeError(
                    "Could not find tweet text box. Ensure you are logged into X in this Firefox profile."
                )

            # Optional media upload (video or image)
            if media_to_upload:
                if verbose:
                    info(f"Uploading {'video' if is_video_post else 'image'} to Twitter...")
                uploaded = self._upload_image(media_to_upload)
                if not uploaded:
                    warning("Media upload failed — posting as text-only.")
                    media_to_upload = None

            post_button = None
            post_button_selectors = [
                (By.XPATH, "//button[@data-testid='tweetButtonInline']"),
                (By.XPATH, "//button[@data-testid='tweetButton']"),
                (By.XPATH, "//span[text()='Post']/ancestor::button"),
            ]

            for selector in post_button_selectors:
                try:
                    post_button = self.wait.until(EC.element_to_be_clickable(selector))
                    self._human_pause(1.0, 2.5)
                    post_button.click()
                    break
                except Exception:
                    continue

            if post_button is None:
                raise RuntimeError("Could not find the Post button on X compose screen.")

            self._human_pause(2.0, 4.0)

            # Cache post with metadata
            post_record = {
                "content": body_text,
                "date": now.strftime("%m/%d/%Y, %H:%M:%S"),
                "reddit_id": reddit_id,
                "reddit_title": reddit_title,
                "reddit_subreddit": subreddit_name,
                "reddit_permalink": selected.get("permalink"),
                "has_image": bool(media_to_upload) and not is_video_post,
                "has_video": bool(media_to_upload) and is_video_post,
            }
            self.add_post(post_record)
            self._mark_reddit_id_used(reddit_id)

            success("Posted localized Reddit meme to Twitter successfully!")
        finally:
            # Best-effort temp cleanup — runs even on exception
            for path in temp_paths.values():
                try:
                    base = os.path.splitext(path)[0]
                    for variant in [base + ".jpg", base + ".png", base + ".gif", base + ".mp4"]:
                        if os.path.exists(variant):
                            os.remove(variant)
                except Exception:
                    pass

    def fetch_and_post_viral(self) -> None:
        """
        Fetches a viral text-based meme/tweet from X and localizes it before posting.
        """
        bot: webdriver.Firefox = self.browser
        verbose: bool = get_verbose()

        if verbose:
            info(" => Navigating to X search for viral text memes...")

        # min_faves 50000, lang en, no links, no media
        search_url = "https://x.com/search?q=min_faves%3A50000%20lang%3Aen%20-filter%3Alinks%20-filter%3Amedia&src=typed_query&f=top"
        bot.get(search_url)

        # wait for tweets to load
        time.sleep(5)
        self._dismiss_popups()
        
        tweet_elements = []
        try:
            # Wait until at least one tweet is present
            self.wait.until(EC.presence_of_element_located((By.XPATH, "//div[@data-testid='tweetText']")))
            bot.execute_script("window.scrollTo(0, 500);")
            time.sleep(2) # Load some more
            tweet_elements = bot.find_elements(By.XPATH, "//div[@data-testid='tweetText']")
        except Exception as e:
            error(f"Could not fetch viral tweets from search: {e}")
            return

        if not tweet_elements:
            error("No viral tweets found on the search page.")
            return

        # Fetch cache to avoid duplicate original texts
        cached_posts = self.get_posts()
        used_originals = [p.get("original_text", "") for p in cached_posts if "original_text" in p]

        selected_text = None
        for el in tweet_elements:
            try:
                text = el.text.strip()
                if len(text) > 10 and text not in used_originals:
                    selected_text = text
                    break
            except Exception:
                continue

        if not selected_text:
            error("No unique viral tweets found (all were cached or invalid).")
            return

        if verbose:
            info(f" => Found viral tweet: {selected_text[:50]}...")
            info(" => Localizing with LLM...")

        prompt = (
            f"Aşağıdaki viral İngilizce tweeti/memeyi al ve Twitter (X) Türkiye kullanıcıları için en organik, "
            f"esprili ve doğal Türk internet argosuyla yerelleştir. Sadece basit bir çeviri yapma, Türkiye "
            f"gündemiyle/kültürüyle uyumlu hale getir. Yalnızca son tweet metnini döndür. KESİNLİKLE EMOJİ KULLANMA.\n\n"
            f"Orijinal Tweet:\n\"{selected_text}\""
        )
        
        completion = generate_text(prompt)
        
        if not completion:
            error("Failed to generate localized post. Please try again.")
            return

        # Clean output
        completion = re.sub(r"\*", "", completion).replace('"', "").strip()
        
        if len(completion) >= 260:
            completion = completion[:257].rsplit(" ", 1)[0] + "..."

        if verbose:
            info(f" => Localized Post: {completion[:50]}...")

        # Post it
        self.post(completion)

        # Update the last post in cache to include original_text to prevent duplicates
        try:
            with open(get_twitter_cache_path(), "r") as file:
                previous_json = json.loads(file.read())
            
            for account in previous_json["accounts"]:
                if account["id"] == self.account_uuid:
                    if len(account["posts"]) > 0:
                        account["posts"][-1]["original_text"] = selected_text
            
            with open(get_twitter_cache_path(), "w") as f:
                json.dump(previous_json, f, indent=4)
        except Exception as e:
            if verbose:
                error(f"Failed to append original_text to cache: {e}")

    def _get_used_replied_ids(self) -> list:
        if not os.path.exists(get_twitter_cache_path()):
            return []
        try:
            with open(get_twitter_cache_path(), "r") as file:
                parsed = json.load(file)
        except Exception:
            return []
        for account in parsed.get("accounts", []):
            if account.get("id") == self.account_uuid:
                return account.get("used_replied_ids", [])
        return []

    def _mark_replied_id_used(self, tweet_id: str) -> None:
        if not os.path.exists(get_twitter_cache_path()):
            with open(get_twitter_cache_path(), "w") as file:
                json.dump({"accounts": []}, file, indent=4)
        with open(get_twitter_cache_path(), "r") as file:
            previous_json = json.load(file)
        accounts = previous_json.get("accounts", [])
        target_account = None
        for account in accounts:
            if account.get("id") == self.account_uuid:
                target_account = account
                break
        if target_account is None:
            target_account = {
                "id": self.account_uuid,
                "nickname": self.account_nickname,
                "firefox_profile": self.fp_profile_path,
                "topic": self.topic,
                "posts": [],
                "used_reddit_ids": [],
                    "used_replied_ids": []
            }
            accounts.append(target_account)

        used_ids = target_account.get("used_replied_ids", [])
        if tweet_id not in used_ids:
            used_ids.append(tweet_id)
        target_account["used_replied_ids"] = used_ids

        previous_json["accounts"] = accounts
        with open(get_twitter_cache_path(), "w") as f:
            json.dump(previous_json, f, indent=4)

    @staticmethod
    def _parse_engagement_count(raw: str) -> int:
        """
        Parse an engagement number string from Twitter aria-labels.
        Handles formats like:
          - "5234"          → 5234
          - "5.234"         → 5234   (Turkish thousands separator)
          - "5,234"         → 5234   (English thousands separator)
          - "12.4K"/"12,4K" → 12400
          - "1.2M"/"1,2M"  → 1200000
          - "3B"            → 3000000000 (English billion) — unlikely on TR
          - "3 B"           → 3000   (Turkish 'bin' = thousand — handled by caller)
        Returns 0 if unparseable.
        """
        s = raw.strip().upper()
        if not s:
            return 0

        multiplier = 1
        # Turkish: "B" = bin (thousand), "Mn" = milyon; English: "K" = thousand, "M" = million
        if s.endswith("MN"):
            multiplier = 1_000_000
            s = s[:-2]
        elif s.endswith("M"):
            multiplier = 1_000_000
            s = s[:-1]
        elif s.endswith("K"):
            multiplier = 1_000
            s = s[:-1]
        elif s.endswith("B"):
            # Turkish "B" = bin = thousand (e.g. "12,4B" = 12400)
            multiplier = 1_000
            s = s[:-1]

        # Normalize decimal separators: Turkish uses comma as decimal, period as thousands
        # If multiplier > 1, the separator is a decimal point (e.g. "12.4K" or "12,4K")
        if multiplier > 1:
            s = s.replace(",", ".")
            try:
                return int(float(s) * multiplier)
            except (ValueError, OverflowError):
                return 0
        else:
            # No suffix — raw integer with possible thousands separators
            s = s.replace(".", "").replace(",", "")
            try:
                return int(s)
            except (ValueError, OverflowError):
                return 0

    def _is_promoted_tweet(self, el) -> bool:
        """
        Check if a tweet article element is a promoted/ad tweet.
        Uses multiple detection strategies for reliability.
        """
        # Strategy 1: Twitter's own tracking testid for ads
        try:
            tracking = el.find_elements(By.XPATH, ".//*[@data-testid='placementTracking']")
            if tracking:
                return True
        except Exception:
            pass

        # Strategy 2: Check for promo labels in the social context area
        # Twitter shows "Promoted" / "Sponsorlu" / "Tanıtılan" as a small label
        promo_keywords = [
            "Promoted", "Sponsorlu", "Tanıtılan", "Tanıtım",
            "Gesponsert", "Sponsorisé", "Promocionado",
        ]
        try:
            # The promo label sits inside a specific span — look for exact or near-exact matches
            social_context = el.find_elements(
                By.XPATH,
                ".//div[@data-testid='socialContext']//span | "
                ".//span[@data-testid='annotationText']"
            )
            for span in social_context:
                span_text = span.text.strip()
                if span_text in promo_keywords:
                    return True
        except Exception:
            pass

        # Strategy 3: Broader sweep — check all small spans for exact promo text
        # (do NOT use contains() to avoid false positives like "Adam")
        try:
            all_spans = el.find_elements(By.XPATH, ".//span")
            for span in all_spans:
                text = span.text.strip()
                if text in promo_keywords:
                    return True
        except Exception:
            pass

        # Strategy 4: Check for ad disclosure links (i.e. "Why this ad?" / "Bu reklam neden gösteriliyor?")
        try:
            ad_links = el.find_elements(
                By.XPATH,
                ".//a[contains(@href, 'ads_transparency')] | "
                ".//a[contains(@href, 'why-this-ad')]"
            )
            if ad_links:
                return True
        except Exception:
            pass

        return False

    def _find_viral_tweet_from_timeline(self) -> Optional[dict]:
        """
        Navigate to X home timeline, scroll, and find a suitable viral tweet to interact with.
        Returns dict with keys: tweet_id, tweet_url, tweet_text; or None if no suitable tweet found.
        """
        bot: webdriver.Firefox = self.browser
        verbose: bool = get_verbose()

        if verbose:
            info(" => Navigating to X home timeline...")

        bot.get("https://x.com/home")
        time.sleep(5)
        self._dismiss_popups()

        # Scroll more aggressively to load a bigger pool of candidate tweets
        try:
            self.wait.until(EC.presence_of_element_located((By.XPATH, "//article[@data-testid='tweet']")))
            scroll_count = random.randint(4, 7)
            for _ in range(scroll_count):
                bot.execute_script("window.scrollBy(0, 700);")
                time.sleep(random.uniform(1.5, 3.0))
                self._dismiss_popups()
        except Exception as e:
            error(f"Could not load timeline tweets: {e}")
            return None

        tweet_elements = bot.find_elements(By.XPATH, "//article[@data-testid='tweet']")
        if not tweet_elements:
            error("No tweets found on the timeline.")
            return None

        if verbose:
            info(f" => Found {len(tweet_elements)} tweet elements on timeline.")

        used_replied = self._get_used_replied_ids()

        # Minimum like threshold — lowered for Turkish timeline compatibility
        MIN_LIKES = 100

        for el in tweet_elements:
            try:
                # Get tweet URL from the timestamp link
                time_link = el.find_element(By.XPATH, ".//a[contains(@href, '/status/')]")
                tweet_url = time_link.get_attribute("href")
                t_id = tweet_url.split("/status/")[1].split("/")[0].split("?")[0]

                if t_id in used_replied:
                    continue

                # Skip promoted / ad tweets with robust detection
                if self._is_promoted_tweet(el):
                    if verbose:
                        info(f"Skipping tweet {t_id} — promoted/ad tweet.")
                    continue

                # Skip grey checkmark accounts (Government/Official)
                try:
                    is_grey = False

                    verified_icons = el.find_elements(By.XPATH, ".//svg[@data-testid='icon-verified']")
                    for icon in verified_icons:
                        icon_html = icon.get_attribute("outerHTML").lower()
                        if any(color in icon_html for color in [
                            "#829aab", "rgb(130, 154, 171)", "rgb(130,154,171)",
                            "#71767b", "rgb(113, 118, 123)", "rgb(113,118,123)"
                        ]):
                            is_grey = True
                            break

                    if not is_grey:
                        user_name_divs = el.find_elements(By.XPATH, ".//div[@data-testid='User-Name']")
                        if user_name_divs:
                            user_html = user_name_divs[0].get_attribute("innerHTML").lower()
                            if any(badge in user_html for badge in [
                                "government account", "devlet", "resmi hesap"
                            ]):
                                is_grey = True

                    if is_grey:
                        if verbose:
                            info(f"Skipping tweet {t_id} — grey/official checkmark account.")
                        continue
                except Exception:
                    pass

                # Parse engagement count from like button aria-label
                like_count = 0
                try:
                    like_btn = el.find_element(By.XPATH, ".//button[@data-testid='like']")
                    aria = like_btn.get_attribute("aria-label") or ""
                    # Aria-label format examples:
                    #   EN: "12400 Likes. Like"  /  "1.2K Likes. Like"
                    #   TR: "5234 Beğeni. Beğen"  /  "12,4B Beğeni. Beğen"
                    # Extract the first token that looks numeric
                    for token in aria.strip().split():
                        parsed = self._parse_engagement_count(token)
                        if parsed > 0:
                            like_count = parsed
                            break
                except Exception:
                    pass

                if like_count < MIN_LIKES:
                    if verbose and like_count > 0:
                        info(f"Skipping tweet {t_id} — likes ({like_count}) below threshold ({MIN_LIKES}).")
                    continue

                # Get tweet text
                tweet_text_el = el.find_element(By.XPATH, ".//div[@data-testid='tweetText']")
                text = tweet_text_el.text.strip()
                if len(text) < 15:
                    continue

                if verbose:
                    info(f" => Found suitable tweet (likes: {like_count}): {text[:50]}...")

                return {
                    "tweet_id": t_id,
                    "tweet_url": tweet_url,
                    "tweet_text": text,
                }
            except Exception:
                continue

        return None

    def reply_to_viral_tweets(self) -> None:
        """
        Finds viral tweets from the home timeline and replies to one of them.
        """
        bot: webdriver.Firefox = self.browser
        verbose: bool = get_verbose()

        tweet_data = self._find_viral_tweet_from_timeline()
        if not tweet_data:
            error("No valid unreplied viral tweets found on timeline.")
            return

        selected_tweet_id = tweet_data["tweet_id"]
        selected_tweet_url = tweet_data["tweet_url"]
        selected_tweet_text = tweet_data["tweet_text"]

        if verbose:
            info(f" => Selected tweet to reply to: {selected_tweet_url}")
            info(f" => Original Tweet Text: {selected_tweet_text[:50]}...")
            
        prompt = (
            "Sen çok yüksek etkileşimli, espritüel ve sevilen genç bir erkek Türk Gen-Z Twitter (X) kullanıcısısın. "
            "Aşağıda verilen popüler, viral Türkçe tweete atacağın organik, zekice ve kısa bir yanıt (reply) oluştur (maksimum 1 cümle). "
            "Yanıtını her zaman rahat takılan genç bir İstanbullu erkeğin ağzından, maskülen/delikanlı ama esprili bir dille yaz. "
            "Asla 'evet haklısın', 'katılıyorum' gibi bot tarzı klişe yanıtlar verme. Özgün, viral olma potansiyeli yüksek, laf sokmalı, övücü veya çok absürt komik bir şey olsun.\n\n"
            f"Orijinal Tweet: \"{selected_tweet_text}\"\n\n"
            "ÇOK ÖNEMLİ: Hiçbir açıklama, giriş cümlesi veya yorum EKLEME! KESİNLİKLE EMOJI KULLANMA! "
            "Noktalama işaretlerini minimum düzeyde kullan. Twitter'da insanlar cümle sonu nokta koymaz virgülü de nadiren kullanır. "
            "Yalnızca JSON formatında yanıt ver. "
            "Format: {\"tweet\": \"<sadece üretilen yanıt>\"}"
        )

        completion = generate_text(prompt)
        if not completion:
            error("Failed to generate a reply post. Please try again.")
            return
            
        try:
            # Temizleme: Eğer AI markdown eklediyse çıkart.
            json_str = completion
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
                
            start_idx = json_str.find("{")
            end_idx = json_str.rfind("}") + 1
            if start_idx != -1 and end_idx != 0:
                json_str = json_str[start_idx:end_idx]
                
            parsed = json.loads(json_str)
            completion = parsed.get("tweet", completion)
        except Exception:
            pass

        completion = re.sub(r"\*", "", completion).replace('"', "").strip()
        if len(completion) > 260:
             completion = completion[:257].rsplit(" ", 1)[0] + "..."

        if verbose:
            info(f" => Generated Reply: {completion}")

        bot.get(selected_tweet_url)
        self._human_pause(3.0, 5.0)
        self._dismiss_popups()

        reply_box_selectors = [
            (By.CSS_SELECTOR, "div[data-testid='tweetTextarea_0'][role='textbox']"),
            (By.XPATH, "//div[@data-testid='tweetTextarea_0']//div[@role='textbox']")
        ]
        
        reply_box = None
        for selector in reply_box_selectors:
            try:
                reply_box = WebDriverWait(bot, 10).until(EC.element_to_be_clickable(selector))
                bot.execute_script("arguments[0].scrollIntoView({block: 'center'});", reply_box)
                self._human_pause(0.5, 1.5)
                reply_box.click()
                self._human_type(reply_box, completion)
                break
            except Exception:
                continue

        if not reply_box:
            error("Could not find the reply text box on the tweet page.")
            return

        reply_btn_selectors = [
            (By.XPATH, "//button[@data-testid='tweetButtonInline']"),
            (By.XPATH, "//span[text()='Reply']/ancestor::button"),
            (By.XPATH, "//span[text()='Yanıtla']/ancestor::button")
        ]
        
        reply_btn = None
        for selector in reply_btn_selectors:
            try:
                reply_btn = WebDriverWait(bot, 5).until(EC.element_to_be_clickable(selector))
                self._human_pause(1.0, 2.0)
                reply_btn.click()
                break
            except Exception:
                continue

        if not reply_btn:
             error("Could not find the Reply button to post.")
             return

        self._human_pause(2.0, 4.0)

        now: datetime = datetime.now()
        post_record = {
            "content": f"[REPLY] {completion}",
            "date": now.strftime("%m/%d/%Y, %H:%M:%S"),
            "is_reply": True,
            "replied_tweet_id": selected_tweet_id,
            "original_text": selected_tweet_text
        }
        self.add_post(post_record)
        self._mark_replied_id_used(selected_tweet_id)

        success("Replied to viral tweet successfully!")

    def quote_viral_tweet(self) -> None:
        """
        Finds viral tweets from the home timeline and quotes one of them.
        """
        bot: webdriver.Firefox = self.browser
        verbose: bool = get_verbose()

        tweet_data = self._find_viral_tweet_from_timeline()
        if not tweet_data:
            error("No valid viral tweets found on timeline for quoting.")
            return

        selected_tweet_id = tweet_data["tweet_id"]
        selected_tweet_url = tweet_data["tweet_url"]
        selected_tweet_text = tweet_data["tweet_text"]

        if verbose:
            info(f" => Selected tweet to quote: {selected_tweet_url}")
            info(f" => Original Tweet Text: {selected_tweet_text[:50]}...")

        prompt = (
            "Sen \u00e7ok y\u00fcksek etkile\u015fimli, esprit\u00fcel ve sevilen gen\u00e7 bir erkek T\u00fcrk Gen-Z Twitter (X) kullan\u0131c\u0131s\u0131s\u0131n. "
            "A\u015fa\u011f\u0131da verilen pop\u00fcler, viral T\u00fcrk\u00e7e tweeti al\u0131nt\u0131layarak ataca\u011f\u0131n organik, zekice ve k\u0131sa bir yorum olu\u015ftur (maksimum 1 c\u00fcmle). "
            "Yorumunu her zaman rahat tak\u0131lan gen\u00e7 bir \u0130stanbullu erke\u011fin a\u011fz\u0131ndan, mask\u00fclen/delikanlı ama esprili bir dille yaz. "
            "Al\u0131nt\u0131lad\u0131\u011f\u0131n tweete bir bak\u0131\u015f a\u00e7\u0131s\u0131 kat, laf sok, espri yap veya abs\u00fcrt bir \u015fekilde yorumla.\n\n"
            f"Orijinal Tweet: \"{selected_tweet_text}\"\n\n"
            "\u00c7OK \u00d6NEML\u0130 KURALLAR:\n"
            "- Hi\u00e7bir a\u00e7\u0131klama, giri\u015f c\u00fcmlesi veya yorum EKLEME\n"
            "- KES\u0130NL\u0130KLE EMOJI KULLANMA\n"
            "- Noktalama i\u015faretlerini minimum d\u00fczeyde kullan. Twitter'da insanlar c\u00fcmle sonu nokta koymaz virg\u00fcl\u00fc de nadiren kullan\u0131r\n"
            "- Yaln\u0131zca JSON format\u0131nda yan\u0131t ver. Format: {\"tweet\": \"<sadece \u00fcretilen yorum>\"}"
        )

        completion = generate_text(prompt)
        if not completion:
            error("Failed to generate a quote tweet. Please try again.")
            return

        try:
            json_str = completion
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            start_idx = json_str.find("{")
            end_idx = json_str.rfind("}") + 1
            if start_idx != -1 and end_idx != 0:
                json_str = json_str[start_idx:end_idx]

            parsed = json.loads(json_str)
            completion = parsed.get("tweet", completion)
        except Exception:
            pass

        completion = re.sub(r"\*", "", completion).replace('"', "").strip()
        if len(completion) > 200:
            completion = completion[:197].rsplit(" ", 1)[0] + "..."

        if verbose:
            info(f" => Generated Quote: {completion}")

        # Navigate to the tweet page and use native Quote flow
        bot.get(selected_tweet_url)
        self._human_pause(3.0, 5.0)
        self._dismiss_popups()

        # Click the retweet button to open the menu
        retweet_btn = None
        try:
            retweet_btn = WebDriverWait(bot, 10).until(
                EC.element_to_be_clickable((By.XPATH, "//button[@data-testid='retweet']"))
            )
            self._human_pause(0.5, 1.5)
            retweet_btn.click()
        except Exception:
            # Maybe already retweeted — try unretweet button
            try:
                retweet_btn = WebDriverWait(bot, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[@data-testid='unretweet']"))
                )
                self._human_pause(0.5, 1.5)
                retweet_btn.click()
                # After unretweeting, need to click retweet again
                self._human_pause(1.0, 2.0)
                retweet_btn = WebDriverWait(bot, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[@data-testid='retweet']"))
                )
                retweet_btn.click()
            except Exception as e:
                error(f"Could not find retweet button: {e}")
                return

        self._human_pause(1.0, 2.0)

        # Select "Quote" from the dropdown menu
        quote_option = None
        quote_selectors = [
            (By.XPATH, "//a[contains(@href, '/compose/post')]//span[contains(text(), 'Quote')]"),
            (By.XPATH, "//a[contains(@href, '/compose/post')]//span[contains(text(), 'Alıntıla')]"),
            (By.XPATH, "//a[contains(@href, '/compose/post')]"),
            (By.XPATH, "//div[@data-testid='Dropdown']//a[contains(@href, '/compose/')]"),
        ]

        for selector in quote_selectors:
            try:
                quote_option = WebDriverWait(bot, 5).until(
                    EC.element_to_be_clickable(selector)
                )
                self._human_pause(0.5, 1.0)
                quote_option.click()
                break
            except Exception:
                continue

        if quote_option is None:
            error("Could not find the 'Quote' option in the retweet menu.")
            return

        self._human_pause(2.0, 4.0)

        # Type the quote comment into the compose box
        text_box = None
        text_box_selectors = [
            (By.CSS_SELECTOR, "div[data-testid='tweetTextarea_0'][role='textbox']"),
            (By.XPATH, "//div[@data-testid='tweetTextarea_0']//div[@role='textbox']"),
            (By.XPATH, "//div[@role='textbox']"),
        ]

        for selector in text_box_selectors:
            try:
                text_box = self.wait.until(EC.element_to_be_clickable(selector))
                text_box.click()
                self._human_pause(0.5, 1.5)
                self._human_type(text_box, completion)
                break
            except Exception:
                continue

        if text_box is None:
            error("Could not find tweet text box for quote tweet.")
            return

        # Wait for the quote card preview to load
        self._human_pause(2.0, 3.0)

        post_button = None
        post_button_selectors = [
            (By.XPATH, "//button[@data-testid='tweetButtonInline']"),
            (By.XPATH, "//button[@data-testid='tweetButton']"),
            (By.XPATH, "//span[text()='Post']/ancestor::button"),
            (By.XPATH, "//span[text()='Göndermek']/ancestor::button"),
        ]

        for selector in post_button_selectors:
            try:
                post_button = self.wait.until(EC.element_to_be_clickable(selector))
                self._human_pause(1.0, 2.5)
                post_button.click()
                break
            except Exception:
                continue

        if post_button is None:
            error("Could not find the Post button for quote tweet.")
            return

        self._human_pause(2.0, 4.0)

        now: datetime = datetime.now()
        post_record = {
            "content": f"[QUOTE] {completion}",
            "date": now.strftime("%m/%d/%Y, %H:%M:%S"),
            "is_quote": True,
            "quoted_tweet_id": selected_tweet_id,
            "quoted_tweet_url": selected_tweet_url,
            "original_text": selected_tweet_text
        }
        self.add_post(post_record)
        self._mark_replied_id_used(selected_tweet_id)

        success("Quote tweeted successfully!")

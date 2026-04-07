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
    download_image,
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
            "The Limit is 2 sentences. Choose a specific sub-topic of the provided topic."
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
            "- 1-3 emoji kullan (doğal görünmeli)\n"
            "- Hashtag KULLANMA\n"
            "- Sadece tweet metnini yaz, başka hiçbir şey ekleme\n"
            "- Alıntı işareti ve yıldız (*) kullanma"
        )

    def fetch_and_post_viral_meme(self) -> None:
        """
        Full pipeline: Reddit → LLM localization → optional image anon → Twitter post.
        """
        verbose = get_verbose()

        # 0. Shadowban protection rules (quota + spacing)
        if not self._enforce_shadowban_rules():
            return

        # 1. Choose subreddit and fetch posts
        subreddit = random.choice(MEME_SUBREDDITS)
        if verbose:
            info(f"Fetching viral posts from r/{subreddit}...")

        posts = fetch_viral_posts(subreddit, limit=25, time_filter="day")
        if not posts:
            # Fallback to week if day fails
            if verbose:
                warning("No posts found for 'day', trying 'week'...")
            posts = fetch_viral_posts(subreddit, limit=25, time_filter="week")

        if not posts:
            error("No suitable Reddit posts found.")
            return

        # 2. Cache-based duplicate guard
        used_ids = self._get_used_reddit_ids()
        selected = select_best_post(posts, used_ids)
        if not selected:
            error("All Reddit candidates were already used. Try again later.")
            return

        # 2.b Simple subreddit rotation: avoid using same subreddit twice in a row if possible
        last_meme = self._get_last_meme_post()

        reddit_id = selected["id"]
        reddit_title = selected["title"]
        subreddit_name = selected["subreddit"]
        image_url = selected["image_url"]

        if last_meme and last_meme.get("reddit_subreddit") == subreddit_name:
            # Try to pick an alternative subreddit post if available
            alt = [p for p in posts if p.get("subreddit") != subreddit_name and p.get("id") not in used_ids]
            if alt:
                alt_selected = select_best_post(alt, used_ids)
                if alt_selected:
                    selected = alt_selected
                    reddit_id = selected["id"]
                    reddit_title = selected["title"]
                    subreddit_name = selected["subreddit"]
                    image_url = selected["image_url"]

        if verbose:
            info(f"Selected Reddit post: [{subreddit_name}] {reddit_title[:80]}...")

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

        # 4/5. Optional image download + anonymization
        temp_paths = get_temp_image_paths()
        image_to_upload = None

        # Decide whether we WANT an image for this run (60/40 mix rule)
        want_image = image_url and self._should_use_image_this_time()

        if want_image:
            if verbose:
                info(f"Downloading image from Reddit: {image_url}")
            # Determine correct extension from URL
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
                # Skip anonymization for GIFs (would destroy animation)
                if ext == ".gif":
                    image_to_upload = raw_path
                    if verbose:
                        info("GIF detected — skipping anonymization to preserve animation.")
                else:
                    try:
                        anon_path = anonymize_image(raw_path, anon_save_path)
                        image_to_upload = anon_path
                        if verbose:
                            info("Image anonymization succeeded.")
                    except Exception as e:
                        warning(f"Image anonymization failed, using raw image: {e}")
                        image_to_upload = raw_path
            else:
                warning("Image download failed. Proceeding with text-only tweet.")

        # 6–12. Compose and post on Twitter with human-like behavior
        try:
            bot: webdriver.Firefox = self.browser
            bot.get("https://x.com/compose/post")
            self._human_pause(2.0, 4.0)

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

            # Optional image upload — check return value
            if image_to_upload:
                if verbose:
                    info("Uploading anonymized image to Twitter...")
                uploaded = self._upload_image(image_to_upload)
                if not uploaded:
                    warning("Image upload failed — posting as text-only.")
                    image_to_upload = None

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
                "has_image": bool(image_to_upload),
            }
            self.add_post(post_record)
            self._mark_reddit_id_used(reddit_id)

            success("Posted localized Reddit meme to Twitter successfully!")
        finally:
            # 14. Best-effort temp cleanup — runs even on exception
            for path in temp_paths.values():
                try:
                    # Also clean extension-variant temp files
                    base = os.path.splitext(path)[0]
                    for variant in [base + ".jpg", base + ".png", base + ".gif"]:
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
            f"gündemiyle/kültürüyle uyumlu hale getir. Yalnızca son tweet metnini döndür.\n\n"
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

    def reply_to_viral_tweets(self) -> None:
        """
        Finds viral Turkish tweets (100k+ views approx 2500+ likes) and replies to one of them.
        """
        bot: webdriver.Firefox = self.browser
        verbose: bool = get_verbose()

        if verbose:
            info(" => Navigating to X search for viral tweets to reply...")

        search_url = "https://x.com/search?q=min_faves%3A2500%20lang%3Atr%20-filter%3Alinks%20-filter%3Areplies&src=typed_query&f=top"
        bot.get(search_url)

        time.sleep(5)
        
        tweet_elements = []
        try:
            self.wait.until(EC.presence_of_element_located((By.XPATH, "//article[@data-testid='tweet']")))
            bot.execute_script("window.scrollTo(0, 800);")
            time.sleep(3)
            tweet_elements = bot.find_elements(By.XPATH, "//article[@data-testid='tweet']")
        except Exception as e:
            error(f"Could not fetch viral tweets for reply: {e}")
            return

        if not tweet_elements:
            error("No viral tweets found on the search page.")
            return

        used_replied = self._get_used_replied_ids()

        selected_tweet_url = None
        selected_tweet_text = None
        selected_tweet_id = None

        for el in tweet_elements:
            try:
                time_link = el.find_element(By.XPATH, ".//a[contains(@href, '/status/')]")
                tweet_url = time_link.get_attribute("href")
                t_id = tweet_url.split("/status/")[1].split("/")[0].split("?")[0]

                if t_id in used_replied:
                    continue

                # Skip grey checkmark accounts (Government/Official)
                try:
                    user_name_elements = el.find_elements(By.XPATH, ".//div[@data-testid='User-Name']")
                    is_grey = False
                    if user_name_elements:
                        user_html = user_name_elements[0].get_attribute("innerHTML").lower()
                        # X often adds specific labels or color hex codes for grey ticks
                        if 'data-testid="icon-verified"' in user_html:
                            if any(word in user_html for word in ["government", "devlet", "resmi", "official", "kurum", "#829", "rgb(130", "rgb(113", "#717"]):
                                is_grey = True

                    if is_grey:
                        if verbose:
                            info(f"Skipping tweet {t_id} as it belongs to a grey/official checkmark account.")
                        continue
                except Exception:
                    pass

                tweet_text_el = el.find_element(By.XPATH, ".//div[@data-testid='tweetText']")
                text = tweet_text_el.text.strip()
                
                if len(text) > 15:
                    selected_tweet_id = t_id
                    selected_tweet_url = tweet_url
                    selected_tweet_text = text
                    break
            except Exception:
                continue

        if not selected_tweet_id:
            error("No valid unreplied viral tweets found.")
            return

        if verbose:
            info(f" => Selected tweet to reply to: {selected_tweet_url}")
            info(f" => Original Tweet Text: {selected_tweet_text[:50]}...")
            
        prompt = (
            "Sen çok yüksek etkileşimli, espritüel ve sevilen genç bir erkek Türk Gen-Z Twitter (X) kullanıcısısın. "
            "Aşağıda verilen popüler, viral Türkçe tweete atacağın organik, zekice ve kısa bir yanıt (reply) oluştur (maksimum 1 cümle). "
            "Yanıtını her zaman rahat takılan genç bir İstanbullu erkeğin ağzından, maskülen/delikanlı ama esprili bir dille yaz. "
            "Asla 'evet haklısın', 'katılıyorum' gibi bot tarzı klişe yanıtlar verme. Özgün, viral olma potansiyeli yüksek, laf sokmalı, övücü veya çok absürt komik bir şey olsun.\n\n"
            f"Orijinal Tweet: \"{selected_tweet_text}\"\n\n"
            "ÇOK ÖNEMLİ: Hiçbir açıklama, giriş cümlesi veya yorum EKLEME! KESİNLİKLE EMOJI KULLANMA! Yalnızca JSON formatında yanıt ver. "
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

import os
from selenium import webdriver
from selenium.webdriver.chrome.service import Service


def build_chrome_driver(
    *,
    headless_env_var: str = "SELENIUM_HEADLESS",
    chrome_binary_env_var: str = "CHROME_BINARY",
    chromedriver_path_env_var: str = "CHROMEDRIVER_PATH",
    user_agent: str | None = None,
    window_size: str = "1600,2200",
    lang: str = "en-AU",
    extra_args: list[str] | None = None,
) -> webdriver.Chrome:
    """
    Build a cross-platform Chrome/Chromium Selenium driver.

    Runtime order:
    1. Use explicit environment variables if provided
    2. On Linux/Pi, try standard Chromium/chromedriver paths
    3. Otherwise fall back to Selenium Manager / local browser discovery

    This allows the same scraper code to run:
    - locally on Windows/macOS/Linux
    - on Raspberry Pi/Linux in production
    """

    headless = os.getenv(headless_env_var, "true").strip().lower() in {"1", "true", "yes", "y"}
    chrome_binary = os.getenv(chrome_binary_env_var, "").strip()
    chromedriver_path = os.getenv(chromedriver_path_env_var, "").strip()

    options = webdriver.ChromeOptions()

    if headless:
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument(f"--window-size={window_size}")
    options.add_argument(f"--lang={lang}")

    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")

    if extra_args:
        for arg in extra_args:
            if arg:
                options.add_argument(arg)

    # 1) Explicit env vars always win
    if chrome_binary:
        options.binary_location = chrome_binary

    if chromedriver_path:
        service = Service(chromedriver_path)
        return webdriver.Chrome(service=service, options=options)

    # 2) Raspberry Pi / Linux fallback
    linux_chrome_candidates = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/snap/bin/chromium",
    ]
    linux_driver_candidates = [
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
    ]

    if os.name != "nt":
        chrome_path = next((p for p in linux_chrome_candidates if os.path.isfile(p)), "")
        driver_path = next((p for p in linux_driver_candidates if os.path.isfile(p)), "")

        if chrome_path:
            options.binary_location = chrome_path

        if driver_path:
            service = Service(driver_path)
            return webdriver.Chrome(service=service, options=options)

    # 3) Local Windows / generic fallback
    return webdriver.Chrome(options=options)

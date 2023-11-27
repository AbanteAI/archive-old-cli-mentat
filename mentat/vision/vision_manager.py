import base64
import os
from typing import Optional

import attr
from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


class ScreenshotException(Exception):
    """
    Thrown when a screenshot is attempted before the browser is opened.
    """


@attr.define
class VisionManager:
    driver: webdriver.Chrome | None = attr.field(default=None)

    def _open_browser(self) -> None:
        if self.driver is None or not self.driver_running():
            service = Service(ChromeDriverManager().install())
            self.driver = webdriver.Chrome(service=service)

    def open(self, path: str) -> None:
        self._open_browser()
        self.driver.get(path)  # type: ignore

    def driver_running(self):
        try:
            # This command should fail if the driver is not running
            self.driver.execute_script('return "hello world";')  # type: ignore
            return True
        except NoSuchWindowException:
            return False

    def screenshot(self, path: Optional[str] = None) -> str:
        if path is not None:
            expanded = os.path.abspath(os.path.expanduser(path))
            if os.path.exists(expanded):
                path = "file://" + expanded
            else:
                if not path.startswith("http"):
                    path = "https://" + path
            self.open(path)
        else:
            if self.driver is None:
                raise ScreenshotException("No browser open")

        screenshot_data = self.driver.get_screenshot_as_png()  # type: ignore

        decoded = base64.b64encode(screenshot_data).decode("utf-8")
        image_data = f"data:image/png;base64,{decoded}"

        return image_data

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()

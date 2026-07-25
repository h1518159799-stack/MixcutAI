from .browser_publisher import BrowserPublisher


class TikTokPublisher(BrowserPublisher):
    def __init__(self):
        super().__init__("tiktok")

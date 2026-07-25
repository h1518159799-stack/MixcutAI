from .base import Publisher, PublishingNotEnabledError


class InstagramPublisher(Publisher):
    platform_code = "instagram"
    integration_type = "api"

    def publish(self, video: str, account: dict, metadata: dict):
        raise PublishingNotEnabledError("Instagram Business API 上传尚未启用")

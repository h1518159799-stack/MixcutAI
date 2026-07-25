from .base import Publisher, PublishingNotEnabledError


class FacebookPublisher(Publisher):
    platform_code = "facebook"
    integration_type = "api"

    def publish(self, video: str, account: dict, metadata: dict):
        raise PublishingNotEnabledError("Facebook Page API 上传尚未启用")

from .base import OAuthAdapter


class InstagramAdapter(OAuthAdapter):
    code = "instagram"
    authorization_endpoint = "https://www.facebook.com/dialog/oauth"
    token_endpoint = "https://graph.facebook.com/oauth/access_token"
    scopes = ("instagram_basic", "instagram_content_publish", "pages_show_list")
    use_pkce = False

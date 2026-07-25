from .base import OAuthAdapter


class FacebookAdapter(OAuthAdapter):
    code = "facebook"
    authorization_endpoint = "https://www.facebook.com/dialog/oauth"
    token_endpoint = "https://graph.facebook.com/oauth/access_token"
    scopes = ("pages_show_list", "pages_manage_posts", "pages_read_engagement")
    use_pkce = False

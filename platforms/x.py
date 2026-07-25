from .base import OAuthAdapter


class XAdapter(OAuthAdapter):
    code = "x"
    authorization_endpoint = "https://x.com/i/oauth2/authorize"
    token_endpoint = "https://api.x.com/2/oauth2/token"
    scopes = ("tweet.read", "tweet.write", "users.read", "offline.access")

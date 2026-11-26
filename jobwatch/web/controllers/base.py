"""What every controller shares."""


class Controller:
    """A controller registers its routes on the router; its actions take the request and return a Response."""

    def register(self, router):
        raise NotImplementedError

    @staticmethod
    def safe_next(target, fallback):
        """A local path to continue to (never another site), or the fallback."""
        return target if target.startswith("/") and not target.startswith("//") else fallback

class ShoppingCartError(Exception):
    pass


class NotEnoughQuantitiesAvailable(ShoppingCartError):
    pass

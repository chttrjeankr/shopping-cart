from django import template

register = template.Library()


@register.filter
def get_quantity(cart, item_id):
    return cart.get(str(item_id), 0)

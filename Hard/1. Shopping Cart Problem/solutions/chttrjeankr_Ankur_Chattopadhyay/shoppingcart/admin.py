from django.contrib import admin

from shoppingcart.models import Category, Item, ItemInOrder, Order

# Register your models here.

admin.site.register(Item)
admin.site.register(ItemInOrder)
admin.site.register(Category)
admin.site.register(Order)

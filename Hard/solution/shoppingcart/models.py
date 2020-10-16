import json

from django.core.exceptions import ValidationError
from django.core.serializers import deserialize, serialize
from django.db import models

from shoppingcart.utilities import delivery_cost, order_directory


class Category(models.Model):
    """docstring for Category."""

    name = models.CharField(max_length=30, unique=True)

    def __str__(self):
        return f"Category {self.pk}: {self.name}"

    class Meta:
        verbose_name = "Category"
        verbose_name_plural = "Categories"


class Item(models.Model):
    """docstring for Item."""

    name = models.CharField(max_length=30)
    category = models.ForeignKey("Category", on_delete=models.PROTECT)
    original_price = models.FloatField()
    discount_price = models.FloatField(null=True, blank=True)
    weight_in_gms = models.FloatField()
    available = models.BooleanField(default=True)

    @property
    def actual_price(self):
        return self.discount_price or self.original_price

    @property
    def savings(self):
        if self.discount_price:
            return self.original_price - self.discount_price
        else:
            return 0

    def __str__(self):
        return f"Item {self.pk}: {self.name}"

    class Meta:
        verbose_name = "Item"
        verbose_name_plural = "Items"
        unique_together = [["name", "category"]]


class Order(models.Model):
    """docstring for Order."""

    PAYMENT_CHOICES = [
        ("NETB", "Net Banking"),
        ("COD", "Cash On Delivery"),
        ("CCARD", "Credit Card"),
        ("DCARD", "Debit Card"),
    ]
    DELIVERY_CHOICES = [
        ("TKW", "Takeaway"),
        ("HMD", "Home Delivery"),
    ]
    ORDER_STATUSES = [
        ("TRAN", "In Transit"),
        ("COMP", "Completed"),
        ("CANC", "Cancelled"),
    ]
    PAYMENT_STATUSES = [
        ("INIT", "Initialized"),
        ("SUCC", "Successful"),
        ("CANC", "Cancelled"),
        ("FAIL", "Failure"),
    ]

    order_id = models.CharField(max_length=30, editable=False)
    razorpay_order_id = models.CharField(max_length=20, editable=False)
    razorpay_payment_id = models.CharField(max_length=18, editable=False)
    razorpay_signature = models.CharField(max_length=64, editable=False)
    payment_status = models.CharField(
        max_length=4, choices=PAYMENT_STATUSES, default="INIT"
    )
    payment_error_code = models.TextField(default="NO ERROR")
    billing_date_time = models.DateTimeField(auto_now_add=True)
    order_modified = models.DateTimeField(auto_now=True)
    order_status = models.CharField(
        max_length=4, choices=ORDER_STATUSES, default="TRAN"
    )
    customer_name = models.CharField(max_length=40)
    customer_mobile_no = models.BigIntegerField()
    payment_method = models.CharField(max_length=5, choices=PAYMENT_CHOICES)
    delivery_option = models.CharField(max_length=3, choices=DELIVERY_CHOICES)
    distance_from_shop = models.IntegerField(blank=True, null=True, default=0)
    shipping_address = models.TextField(blank=True, null=True)

    def get_billed_items(self):
        item_list = self.get_items_from_json_cart()
        return item_list

    @property
    def total_tax(self):
        tax_rate = 0.06
        total_price = self.total_item_price
        return round(tax_rate * total_price, 2)

    @property
    def total_shipping(self):
        if self.delivery_option == "HMD":
            try:
                return delivery_cost[
                    filter(
                        lambda x: self.distance_from_shop in x, delivery_cost.keys()
                    ).__next__()
                ]
            except StopIteration:
                return None
        else:
            return 0

    @property
    def total_item_price(self):
        price = 0
        item_list = self.get_billed_items()
        for item, quantity in item_list:
            price += item.actual_price * quantity
        return round(price, 2)

    @property
    def total_savings(self):
        saved = 0
        item_list = self.get_billed_items()
        for item, quantity in item_list:
            saved += item.savings * quantity
        return round(saved, 2)

    @property
    def amount_payable(self):
        return round((self.total_item_price + self.total_tax + self.total_shipping), 2)

    def get_items_from_json_cart(self):
        with open(order_directory + f"cart_{self.order_id}.json") as f:
            cart_list = json.load(f)

        item_list = []
        for item in cart_list[1:]:
            quantity = item.pop("quantity")
            item_deserialized = list(deserialize("json", json.dumps([item])))[0]
            item_obj = item_deserialized.object
            item_list.append((item_obj, quantity))

        return item_list

    def save_cart(self, cart):
        cart_list = [{"order_id": self.order_id}]
        for i, (item, quantity) in enumerate(cart.items()):
            ser_item = Item.objects.filter(pk=item.pk)
            store_cart = json.loads(serialize("json", ser_item))
            store_cart[0]["quantity"] = quantity
            cart_list.extend(store_cart)

        with open(order_directory + f"cart_{self.order_id}.json", "w") as f:
            json.dump(cart_list, f)

    def clean(self):
        if self.total_shipping is None:
            raise ValidationError("Undeliverable Shipping Address")

    def get_razorpay_client(self):
        import razorpay
        import os
        from dotenv import load_dotenv

        load_dotenv()

        RAZORPAY_KEYID = os.getenv("razorpay_keyid")
        RAZORPAY_SECRET = os.getenv("razorpay_secret")

        client = razorpay.Client(auth=(RAZORPAY_KEYID, RAZORPAY_SECRET))
        return client

    def get_razorpay_order_id(self):
        client = self.get_razorpay_client()
        order_amount = self.amount_payable * 100
        order_currency = "INR"
        order_receipt = self.order_id
        notes = {"Shipping Address": self.shipping_address}

        resp = client.order.create(
            data={
                "amount": order_amount,
                "currency": order_currency,
                "receipt": order_receipt,
                "notes": notes,
            }
        )
        return resp["id"]

    def verify_razorpay_signature(self, params_dict):
        client = self.get_razorpay_client()
        # self.razorpay_order_id = params_dict["razorpay_order_id"]
        self.razorpay_payment_id = params_dict["razorpay_payment_id"]
        self.razorpay_signature = params_dict["razorpay_signature"]
        self.save()
        try:
            client.utility.verify_payment_signature(params_dict)
            self.payment_status = "SUCC"
            return True
        except razorpay.errors.SignatureVerificationError:
            self.payment_status = "FAIL"
            self.payment_error_code = "SignatureVerificationError"
            return False
        finally:
            self.save()

    def save(self, cart=None, *args, **kwargs):
        if not self.order_id:
            self.order_id = f"{hash(self.billing_date_time)}"
            print(self.order_id)
            self.save_cart(cart)
            self.razorpay_order_id = self.get_razorpay_order_id()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order {self.order_id}"

    class Meta:
        verbose_name = "Order"
        verbose_name_plural = "Orders"

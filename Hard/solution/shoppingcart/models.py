import uuid

from django.core.exceptions import ValidationError
from django.core.serializers import deserialize, serialize
from django.db import models, transaction
from shoppingcart.exceptions import NotEnoughQuantitiesAvailable
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
    available_quantity = models.IntegerField(default=0)

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


class PurchasedItem(models.Model):
    """docstring for PurchasedItem."""

    item = models.ForeignKey("Item", on_delete=models.PROTECT)
    order = models.ForeignKey("Order", on_delete=models.PROTECT)
    purchased_price = models.FloatField()
    savings = models.FloatField()
    purchased_quantity = models.IntegerField()

    def __str__(self):
        return f"Purchased Item {self.order.order_id}: {self.item.name} [{self.purchased_quantity}]"

    class Meta:
        verbose_name = "Purchased Item"
        verbose_name_plural = "Purchased Items"
        unique_together = [["item", "order"]]


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

    order_uuid = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order_id = models.CharField(max_length=30, editable=False)
    razorpay_order_id = models.CharField(max_length=20, editable=False)
    razorpay_payment_id = models.CharField(max_length=18, editable=False)
    razorpay_signature = models.CharField(max_length=64, editable=False)
    payment_status = models.CharField(
        max_length=4, choices=PAYMENT_STATUSES, default="INIT"
    )
    payment_error_code = models.TextField(default="NoError")
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
        for item in self.items_in_order:
            price += item.purchased_price * item.purchased_quantity
        return round(price, 2)

    @property
    def total_savings(self):
        saved = 0
        for item in self.items_in_order:
            saved += item.savings * item.purchased_quantity
        return round(saved, 2)

    @property
    def amount_payable(self):
        return round((self.total_item_price + self.total_tax + self.total_shipping), 2)

    @property
    def items_in_order(self):
        items_in_order = PurchasedItem.objects.filter(order=self)
        return items_in_order

    def save_cart(self, cart):
        for item, quantity in cart.items():
            if item.available_quantity < quantity:
                raise NotEnoughQuantitiesAvailable
            p_item = PurchasedItem(
                item=item,
                order=self,
                purchased_price=item.actual_price,
                savings=item.savings,
                purchased_quantity=quantity,
            )
            p_item.save()

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

    def capture_razorpay_payment(self):
        from razorpay.errors import BadRequestError

        client = self.get_razorpay_client()
        payment_id = self.razorpay_payment_id
        payment_amount = self.amount_payable * 100
        payment_currency = "INR"

        try:
            resp = client.payment.capture(
                payment_id,
                payment_amount,
                {"currency": payment_currency},
            )
            return True
        except BadRequestError:
            self.payment_status = "FAIL"
            self.payment_error_code = "PaymentCaptureFailure"
            return False
        except Exception as e:
            self.payment_status = "FAIL"
            self.payment_error_code = f"Error:{e}"
            return False
        finally:
            self.save()

    def verify_razorpay_signature(self, params_dict):
        from razorpay.errors import SignatureVerificationError

        client = self.get_razorpay_client()
        self.razorpay_payment_id = params_dict["razorpay_payment_id"]
        self.razorpay_signature = params_dict["razorpay_signature"]
        self.save()

        all_purchased_items = (
            PurchasedItem.objects.filter(order=self)
            .select_related("item")
            .select_for_update()
        )
        # write-lock database for quantity update (atomic transaction)

        # https://docs.djangoproject.com/en/dev/ref/models/querysets/#select-for-update
        with transaction.atomic():
            try:
                # verify payment
                client.utility.verify_payment_signature(params_dict)

                # check for availablity
                for pur_item in all_purchased_items:
                    if pur_item.item.available_quantity < pur_item.purchased_quantity:
                        raise NotEnoughQuantitiesAvailable

                # capture payment
                if self.capture_razorpay_payment():
                    # when verified, deduct from available quantity and open lock
                    for pur_item in all_purchased_items:
                        pur_item.item.available_quantity -= pur_item.purchased_quantity
                        pur_item.item.save()
                    self.payment_status = "SUCC"
                    return True

            except SignatureVerificationError:
                self.payment_status = "FAIL"
                self.payment_error_code = "SignatureVerificationError"
                return False
            except NotEnoughQuantitiesAvailable:
                self.payment_status = "FAIL"
                self.payment_error_code = "NotEnoughQuantitiesAvailable"
                return False
            finally:
                self.save()

    def save(self, cart=None, *args, **kwargs):
        try:
            if not self.order_id:
                self.order_id = f"{hash(self.order_uuid)}"
                # generate order existence to refer as FK in other model saves
                self.save()
                self.save_cart(cart)
                self.razorpay_order_id = self.get_razorpay_order_id()
            super().save(*args, **kwargs)
        except NotEnoughQuantitiesAvailable:
            # if failed in order creation, clean garbage database entry
            self.delete()

    def __str__(self):
        return f"Order {self.order_id}"

    class Meta:
        verbose_name = "Order"
        verbose_name_plural = "Orders"

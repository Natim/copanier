import csv
from pathlib import Path
from time import perf_counter

import ujson as json
import minicli
from jinja2 import Environment, PackageLoader, select_autoescape
from roll import Roll, Response
from roll.extensions import cors, options, traceback, simple_server, static

from . import config, reports, session, utils, emails, loggers
from .models import Delivery, Order, Person, Product, ProductOrder


class Response(Response):
    def html(self, template_name, *args, **kwargs):
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        context = app.context()
        context.update(kwargs)
        context["request"] = self.request
        if self.request.cookies.get("message"):
            context["message"] = json.loads(self.request.cookies["message"])
            self.cookies.set("message", "")
        self.body = env.get_template(template_name).render(*args, **context)

    def redirect(self, location):
        self.status = 302
        self.headers["Location"] = location

    redirect = property(None, redirect)

    def message(self, text, status="success"):
        self.cookies.set("message", json.dumps((text, status)))


class Roll(Roll):
    Response = Response

    _context_func = []

    def context(self):
        context = {}
        for func in self._context_func:
            context.update(func())
        return context

    def register_context(self, func):
        self._context_func.append(func)


env = Environment(
    loader=PackageLoader("copanier", "templates"),
    autoescape=select_autoescape(["copanier"]),
)


def date_filter(value):
    return value.strftime("%d %B")


def time_filter(value):
    return value.strftime("%H:%M")


env.filters["date"] = date_filter
env.filters["time"] = time_filter

app = Roll()
cors(app, methods="*", headers="*")
options(app)


@app.listen("request")
async def auth_required(request, response):
    if not request.route.payload.get("genuine"):
        token = request.cookies.get("token")
        email = None
        if token:
            decoded = utils.read_token(token)
            email = decoded.get("sub")
        if not email:
            response.redirect = f"/sésame?next={request.path}"
            return response
    user = Person(email=email)
    request["user"] = user
    session.user.set(user)


@app.listen("request")
async def attach_request(request, response):
    response.request = request

@app.listen("request")
async def log_request(request, response):
    if request.method == "POST":
        message = {
            "date": utils.utcnow().isoformat(),
            "data": request.form,
            "user": request.get("user"),
        }
        loggers.request_logger.info(
            json.dumps(message, sort_keys=True, ensure_ascii=False)
        )


@app.listen("startup")
async def on_startup():
    configure()
    Delivery.init_fs()


@app.route("/sésame", methods=["GET"], genuine=True)
async def sesame(request, response):
    response.html("sesame.html")


@app.route("/sésame", methods=["POST"])
async def send_sesame(request, response, genuine=True):
    email = request.form.get("email")
    token = utils.create_token(email)
    emails.send(
        email,
        "Sésame Copanier",
        emails.ACCESS_GRANTED.format(hostname=request.host, token=token.decode()),
    )
    response.message(f"Un sésame vous a été envoyé à l'adresse '{email}'")
    response.redirect = "/"


@app.route("/sésame/{token}", methods=["GET"], genuine=True)
async def set_sesame(request, response, token):
    decoded = utils.read_token(token)
    if not decoded:
        response.message("Sésame invalide :(", status="error")
    else:
        response.message("Yay! Le sésame a fonctionné. Bienvenue à bord! :)")
        response.cookies.set(
            name="token", value=token, httponly=True, max_age=60 * 60 * 24 * 7
        )
    response.redirect = "/"


@app.route("/", methods=["GET"])
async def home(request, response):
    response.html("home.html", incoming=Delivery.incoming(), former=Delivery.former())


@app.route("/livraison", methods=["GET"])
async def new_delivery(request, response):
    response.html("edit_delivery.html", delivery={})


@app.route("/livraison", methods=["POST"])
async def create_delivery(request, response):
    form = request.form
    data = {}
    data["from_date"] = f"{form.get('date')} {form.get('from_time')}"
    data["to_date"] = f"{form.get('date')} {form.get('to_time')}"
    for name, field in Delivery.__dataclass_fields__.items():
        if name in form:
            data[name] = form.get(name)
    delivery = Delivery(**data)
    delivery.persist()
    response.message("La livraison a bien été créée!")
    response.redirect = f"/livraison/{delivery.id}"


@app.route("/livraison/{id}/importer/produits", methods=["POST"])
async def import_products(request, response, id):
    delivery = Delivery.load(id)
    delivery.products = []
    reader = csv.DictReader(
        request.files.get("data").read().decode().splitlines(), delimiter=";"
    )
    for row in reader:
        delivery.products.append(Product(**row))
    delivery.persist()
    response.message("Les produits de la livraison ont bien été mis à jour!")
    response.redirect = f"/livraison/{delivery.id}"


@app.route("/livraison/{id}/edit", methods=["GET"])
async def edit_delivery(request, response, id):
    delivery = Delivery.load(id)
    response.html("edit_delivery.html", {"delivery": delivery})


@app.route("/livraison/{id}/edit", methods=["POST"])
async def post_delivery(request, response, id):
    delivery = Delivery.load(id)
    form = request.form
    delivery.from_date = f"{form.get('date')} {form.get('from_time')}"
    delivery.to_date = f"{form.get('date')} {form.get('to_time')}"
    for name, field in Delivery.__dataclass_fields__.items():
        if name in form:
            setattr(delivery, name, form.get(name))
    delivery.persist()
    response.message("La livraison a bien été mise à jour!")
    response.redirect = f"/livraison/{delivery.id}"


@app.route("/livraison/{id}", methods=["GET"])
async def view_delivery(request, response, id):
    delivery = Delivery.load(id)
    response.html("delivery.html", {"delivery": delivery})


@app.route("/livraison/{id}/commander", methods=["POST", "GET"])
async def place_order(request, response, id):
    delivery = Delivery.load(id)
    email = request.query.get("email", None)
    user = session.user.get(None)
    delivery_url = f"/livraison/{delivery.id}"
    if not email and user:
        email = user.email
    if not email:
        response.message("Impossible de comprendre pour qui passer commande…", "error")
        response.redirect = delivery_url
        return
    if request.method == "POST":
        form = request.form
        order = Order(paid=form.bool("paid", False))
        for product in delivery.products:
            quantity = form.int(product.ref, 0)
            if quantity:
                order.products[product.ref] = ProductOrder(wanted=quantity)
        if not delivery.orders:
            delivery.orders = {}
        if not order.products:
            if email in delivery.orders:
                del delivery.orders[email]
                delivery.persist()
            response.message("La commande est vide.", status="warning")
            response.redirect = delivery_url
            return
        delivery.orders[email] = order
        delivery.persist()
        if user and user.email == email:
            # Only send email if order has been placed by the user itself.
            emails.send_order(
                env, person=Person(email=email), delivery=delivery, order=order
            )
        response.message(f"La commande pour «{email}» a bien été prise en compte!")
        response.redirect = f"/livraison/{delivery.id}"
    else:
        order = delivery.orders.get(email) or Order()
        response.html(
            "place_order.html",
            {"delivery": delivery, "person": Person(email=email), "order": order},
        )


@app.route("/livraison/{id}/courriel", methods=["GET"])
async def send_order(request, response, id):
    delivery = Delivery.load(id)
    email = request.query.get("email")
    order = delivery.orders.get(email)
    if not order:
        response.message(f"Aucune commande pour «{email}»", status="warning")
    else:
        emails.send_order(
            env, person=Person(email=email), delivery=delivery, order=order
        )
        response.message(f"Commande envoyée à «{email}»")
    response.redirect = f"/livraison/{delivery.id}"


@app.route("/livraison/{id}/émargement", methods=["GET"])
async def signing_sheet(request, response, id):
    delivery = Delivery.load(id)
    response.html("signing_sheet.html", {"delivery": delivery})


@app.route("/livraison/{id}/importer/commande", methods=["POST"])
async def import_commande(request, response, id):
    email = request.form.get("email")
    order = Order()
    reader = csv.DictReader(
        request.files.get("data").read().decode().splitlines(), delimiter=";"
    )
    for row in reader:
        wanted = int(row["wanted"] or 0)
        if wanted:
            order.products[row["ref"]] = ProductOrder(wanted=wanted)
    delivery = Delivery.load(id)
    delivery.orders[email] = order
    delivery.persist()
    response.message(f"Yallah! La commande de {email} a bien été importée!")
    response.redirect = f"/livraison/{delivery.id}"


@app.route("/livraison/{id}/rapport.xlsx", methods=["GET"])
async def xls_report(request, response, id):
    delivery = Delivery.load(id)
    response.body = reports.summary(delivery)
    mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response.headers["Content-Disposition"] = f'attachment; filename="epinamap.xlsx"'
    response.headers["Content-Type"] = f"{mimetype}; charset=utf-8"


@app.route("/livraison/{id}/rapport-complet.xlsx", methods=["GET"])
async def xls_full_report(request, response, id):
    delivery = Delivery.load(id)
    response.body = reports.full(delivery)
    mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    response.headers["Content-Disposition"] = f'attachment; filename="epinamap.xlsx"'
    response.headers["Content-Type"] = f"{mimetype}; charset=utf-8"


def configure():
    config.init()


@minicli.cli()
def shell():
    """Run an ipython in app context."""
    try:
        from IPython import start_ipython
    except ImportError:
        print('IPython is not installed. Type "pip install ipython"')
    else:
        start_ipython(
            argv=[],
            user_ns={
                "app": app,
                "Product": Product,
                "Person": Person,
                "Order": Order,
                "Delivery": Delivery,
            },
        )


@minicli.wrap
def cli_wrapper():
    configure()
    start = perf_counter()
    yield
    elapsed = perf_counter() - start
    print(f"Done in {elapsed:.5f} seconds.")


@minicli.cli
def serve(reload=False):
    """Run a web server (for development only)."""
    if reload:
        import hupper

        hupper.start_reloader("copanier.serve")
    traceback(app)
    static(app, root=Path(__file__).parent / "static")
    simple_server(app, port=2244)


def main():
    minicli.run()

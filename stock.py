# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.i18n import gettext
from trytond.model import ModelView, fields
from trytond.pool import Pool
from trytond.transaction import Transaction
from trytond.wizard import (
    Button, StateAction, StateTransition, StateView, Wizard)

from .exceptions import SupplyWarning


class Supply(Wizard):
    "Supply Stock"
    __name__ = 'stock.supply'
    start = StateView(
        'stock.supply.start',
        'stock_supply.supply_start_view_form', [
            Button('Cancel', 'end', 'tryton-cancel'),
            Button('Create', 'create_', 'tryton-ok', default=True),
            ])
    create_ = StateTransition()
    internal = StateAction('stock.act_shipment_internal_form')
    purchase = StateAction('purchase_request.act_purchase_request_form')

    @classmethod
    def types(cls):
        return ['internal', 'purchase']

    @classmethod
    def next_action(cls, name):
        types = cls.types()
        try:
            return types[types.index(name) + 1]
        except IndexError:
            return 'end'

    def transition_create_(self):
        pool = Pool()
        Move = pool.get('stock.move')
        ShipmentInternal = pool.get('stock.shipment.internal')
        Date = pool.get('ir.date')
        Warning = pool.get('res.user.warning')
        today = Date.today()
        with Transaction().set_context(_check_access=True):
            if Move.search([
                        ('from_location.type', '=', 'supplier'),
                        ('to_location.type', '=', 'storage'),
                        ('state', '=', 'draft'),
                        ('planned_date', '<', today),
                        ], order=[]):
                name = '%s.supplier@%s' % (self.__name__, today)
                if Warning.check(name):
                    raise SupplyWarning(name,
                        gettext('stock_supply.msg_late_supplier_moves'))
            if Move.search([
                        ('from_location.type', '=', 'storage'),
                        ('to_location.type', '=', 'customer'),
                        ('state', '=', 'draft'),
                        ('planned_date', '<', today),
                        ], order=[]):
                name = '%s..customer@%s' % (self.__name__, today)
                if Warning.check(name):
                    raise SupplyWarning(name,
                        gettext('stock_supply.msg_late_customer_moves'))

        first = True
        created = False
        while created or first:
            created = False
            for type_ in self.types():
                created |= bool(getattr(self, 'generate_%s' % type_)(first))
            first = False

        # Remove transit split of request
        with Transaction().set_context(_check_access=True):
            shipments = ShipmentInternal.search([
                    ('state', '=', 'request'),
                    ])
        Move.delete([m for s in shipments for m in s.moves
                if m.from_location == s.transit_location])
        for shipment in shipments:
            Move.write([m for m in shipment.moves], {
                    'from_location': shipment.from_location.id,
                    'to_location': shipment.to_location.id,
                    'planned_date': shipment.planned_date,
                    })

        return self.types()[0]

    def generate_internal(self, clean):
        pool = Pool()
        ShipmentInternal = pool.get('stock.shipment.internal')
        # Use getattr because start is empty when run by cron
        if getattr(self.start, 'warehouses', None):
            warehouses = self.start.warehouses
        else:
            warehouses = None
        return ShipmentInternal.generate_internal_shipment(
            clean=clean, warehouses=warehouses)

    def transition_internal(self):
        return self.next_action('internal')

    @property
    def _purchase_parameters(self):
        parameters = {}
        # Use getattr because start is empty when run by cron
        if getattr(self.start, 'warehouses', None):
            parameters['warehouses'] = self.start.warehouses
        return parameters

    def generate_purchase(self, clean):
        pool = Pool()
        PurchaseRequest = pool.get('purchase.request')
        PurchaseRequest.generate_requests(**self._purchase_parameters)
        return False

    def transition_purchase(self):
        return self.next_action('purchase')


class SupplyStart(ModelView):
    "Supply Stock"
    __name__ = 'stock.supply.start'

    warehouses = fields.Many2Many(
        'stock.location', None, None, "Warehouses",
        domain=[
            ('type', '=', 'warehouse'),
            ],
        help="If empty all warehouses are used.")

    @classmethod
    def default_warehouses(cls):
        pool = Pool()
        Location = pool.get('stock.location')
        warehouse = Location.get_default_warehouse()
        if warehouse:
            return [warehouse]

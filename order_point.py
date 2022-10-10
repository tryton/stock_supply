# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from sql import Null

from trytond.i18n import gettext
from trytond.model import ModelSQL, ModelView, fields
from trytond.pool import Pool
from trytond.pyson import Equal, Eval, If, In, Not
from trytond.transaction import Transaction

from .exceptions import OrderPointValidationError


class OrderPoint(ModelSQL, ModelView):
    """
    Order Point
    Provide a way to define a supply policy for each
    product on each locations. Order points on warehouse are
    considered by the supply scheduler to generate purchase requests.
    """
    __name__ = 'stock.order_point'
    product = fields.Many2One(
        'product.product', "Product", required=True,
        domain=[
            ('type', '=', 'goods'),
            ('consumable', '=', False),
            ('purchasable', 'in', If(Equal(Eval('type'), 'purchase'),
                    [True], [True, False])),
            ],
        context={
            'company': Eval('company', -1),
            },
        depends={'company'})
    warehouse_location = fields.Many2One(
        'stock.location', 'Warehouse Location',
        domain=[('type', '=', 'warehouse')],
        states={
            'invisible': Not(Equal(Eval('type'), 'purchase')),
            'required': Equal(Eval('type'), 'purchase'),
            })
    storage_location = fields.Many2One(
        'stock.location', "Storage Location",
        domain=[('type', '=', 'storage')],
        states={
            'invisible': Not(Equal(Eval('type'), 'internal')),
            'required': Equal(Eval('type'), 'internal'),
        })
    location = fields.Function(fields.Many2One('stock.location', 'Location'),
            'get_location', searcher='search_location')
    provisioning_location = fields.Many2One(
        'stock.location', 'Provisioning Location',
        domain=[('type', 'in', ['storage', 'view'])],
        states={
            'invisible': Not(Equal(Eval('type'), 'internal')),
            'required': ((Eval('type') == 'internal')
                & (Eval('min_quantity', None) != None)),  # noqa: E711
        })
    overflowing_location = fields.Many2One(
        'stock.location', 'Overflowing Location',
        domain=[('type', 'in', ['storage', 'view'])],
        states={
            'invisible': Eval('type') != 'internal',
            'required': ((Eval('type') == 'internal')
                & (Eval('max_quantity', None) != None)),  # noqa: E711
            })
    type = fields.Selection(
        [('internal', 'Internal'),
         ('purchase', 'Purchase')],
        "Type", required=True)
    min_quantity = fields.Float(
        "Minimal Quantity", digits='unit',
        states={
            # required for purchase and production types
            'required': Eval('type') != 'internal',
            },
        domain=['OR',
            ('min_quantity', '=', None),
            ('min_quantity', '<=', Eval('target_quantity', 0)),
            ])
    target_quantity = fields.Float(
        "Target Quantity", digits='unit', required=True,
        domain=[
            ['OR',
                ('min_quantity', '=', None),
                ('target_quantity', '>=', Eval('min_quantity', 0)),
                ],
            ['OR',
                ('max_quantity', '=', None),
                ('target_quantity', '<=', Eval('max_quantity', 0)),
                ],
            ])
    max_quantity = fields.Float(
        "Maximal Quantity", digits='unit',
        states={
            'invisible': Eval('type') != 'internal',
            },
        domain=['OR',
            ('max_quantity', '=', None),
            ('max_quantity', '>=', Eval('target_quantity', 0)),
            ])
    company = fields.Many2One('company.company', 'Company', required=True,
            domain=[
                ('id', If(In('company', Eval('context', {})), '=', '!='),
                    Eval('context', {}).get('company', -1)),
            ])
    unit = fields.Function(fields.Many2One('product.uom', 'Unit'), 'get_unit')

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        sql_table = cls.__table__()
        table = cls.__table_handler__(module_name)

        # Migration from 4.2
        table.drop_constraint('check_max_qty_greater_min_qty')
        table.not_null_action('min_quantity', 'remove')
        table.not_null_action('max_quantity', 'remove')
        target_qty_exist = table.column_exist('target_quantity')

        super(OrderPoint, cls).__register__(module_name)

        # Migration from 4.2
        if not target_qty_exist:
            cursor.execute(*sql_table.update(
                    [sql_table.target_quantity, sql_table.max_quantity],
                    [sql_table.max_quantity, Null]))

    @staticmethod
    def default_type():
        return "purchase"

    @fields.depends('product', '_parent_product.default_uom')
    def on_change_product(self):
        self.unit = None
        if self.product:
            self.unit = self.product.default_uom

    def get_unit(self, name):
        return self.product.default_uom.id

    @classmethod
    def validate(cls, orderpoints):
        super(OrderPoint, cls).validate(orderpoints)
        cls.check_concurrent_internal(orderpoints)
        cls.check_uniqueness(orderpoints)

    @classmethod
    def check_concurrent_internal(cls, orders):
        """
        Ensure that there is no 'concurrent' internal order
        points. I.E. no two order point with opposite location for the
        same product and same company.
        """
        internals = cls.search([
                ('id', 'in', [o.id for o in orders]),
                ('type', '=', 'internal'),
                ])
        if not internals:
            return

        for location_name in [
                'provisioning_location', 'overflowing_location']:
            query = []
            for op in internals:
                if getattr(op, location_name, None) is None:
                    continue
                arg = ['AND',
                    ('product', '=', op.product.id),
                    (location_name, '=', op.storage_location.id),
                    ('storage_location', '=',
                        getattr(op, location_name).id),
                    ('company', '=', op.company.id),
                    ('type', '=', 'internal')]
                query.append(arg)
            if query and cls.search(['OR'] + query):
                raise OrderPointValidationError(
                    gettext('stock_supply'
                        '.msg_order_point_concurrent_%s_internal' %
                        location_name))

    @staticmethod
    def _type2field(type=None):
        t2f = {
            'purchase': 'warehouse_location',
            'internal': 'storage_location',
            }
        if type is None:
            return t2f
        else:
            return t2f[type]

    @classmethod
    def check_uniqueness(cls, orders):
        """
        Ensure uniqueness of order points. I.E that there is no several
        order point for the same location, the same product and the
        same company.
        """
        query = ['OR']
        for op in orders:
            field = cls._type2field(op.type)
            arg = ['AND',
                ('product', '=', op.product.id),
                (field, '=', getattr(op, field).id),
                ('id', '!=', op.id),
                ('company', '=', op.company.id),
                ]
            query.append(arg)
        if cls.search(query):
            raise OrderPointValidationError(
                gettext('stock_supply.msg_order_point_unique'))

    def get_rec_name(self, name):
        return "%s @ %s" % (self.product.name, self.location.name)

    @classmethod
    def search_rec_name(cls, name, clause):
        return ['OR',
            ('location.rec_name',) + tuple(clause[1:]),
            ('product.rec_name',) + tuple(clause[1:]),
            ]

    def get_location(self, name):
        if self.type == 'purchase':
            return self.warehouse_location.id
        elif self.type == 'internal':
            return self.storage_location.id

    @classmethod
    def search_location(cls, name, domain=None):
        clauses = ['OR']
        for type, field in cls._type2field().items():
            clauses.append([
                    ('type', '=', type),
                    (field,) + tuple(domain[1:]),
                    ])
        return clauses

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @classmethod
    def supply_stock(cls):
        pool = Pool()
        StockSupply = pool.get('stock.supply', type='wizard')
        session_id, _, _ = StockSupply.create()
        StockSupply.execute(session_id, {}, 'create_')
        StockSupply.delete(session_id)

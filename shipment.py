# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import datetime

from sql import Table
from sql.functions import Overlay, Position

from trytond.model import ModelView, ModelSQL
from trytond.transaction import Transaction
from trytond.pool import Pool

__all__ = ['ShipmentInternal']


class ShipmentInternal(ModelSQL, ModelView):
    __name__ = 'stock.shipment.internal'

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        model_data = Table('ir_model_data')
        model = Table('ir_model')
        # Migration from 1.2: packing renamed into shipment
        cursor.execute(*model_data.update(
                columns=[model_data.fs_id],
                values=[Overlay(model_data.fs_id, 'shipment',
                        Position('packing', model_data.fs_id),
                        len('packing'))],
                where=model_data.fs_id.like('%packing%')
                & (model_data.module == module_name)))
        cursor.execute(*model.update(
                columns=[model.model],
                values=[Overlay(model.model, 'shipment',
                        Position('packing', model.model),
                        len('packing'))],
                where=model.model.like('%packing%')
                & (model.module == module_name)))
        super(ShipmentInternal, cls).__register__(module_name)

    @classmethod
    def generate_internal_shipment(cls, clean=True):
        """
        Generate internal shipments to meet order points defined on
        non-warehouse location.

        If clean is set, it will remove all previous requests.
        """
        pool = Pool()
        OrderPoint = pool.get('stock.order_point')
        Location = pool.get('stock.location')
        Product = pool.get('product.product')
        Date = pool.get('ir.date')
        User = pool.get('res.user')
        Move = pool.get('stock.move')
        LeadTime = pool.get('stock.location.lead_time')

        user_record = User(Transaction().user)
        today = Date.today()
        lead_time = LeadTime.get_max_lead_time()

        if clean:
            reqs = cls.search([
                    ('state', '=', 'request'),
                    ])
            cls.delete(reqs)

        # fetch quantities on order points
        order_points = OrderPoint.search([
            ('type', '=', 'internal'),
            ])
        id2product = {}
        product2op = {}
        id2location = {}
        for op in order_points:
            id2product[op.product.id] = op.product
            product2op[
                (op.storage_location.id, op.product.id)
                ] = op
            id2location[op.storage_location.id] = op.storage_location
        provisioned = Location.search([
                ('provisioning_location', '!=', None),
                ])
        id2location.update({l.id: l for l in provisioned})

        # ordered by ids to speedup reduce_ids in products_by_location
        if provisioned:
            products = Product.search([
                    ('type', 'in', ['goods', 'assets']),
                    ], order=[('id', 'ASC')])
            product_ids = [p.id for p in products]
        else:
            product_ids = id2product.keys()
            product_ids.sort()

        shipments = []
        date = today
        end_date = date + lead_time
        while date <= end_date:
            with Transaction().set_context(forecast=True, stock_date_end=date):
                pbl = Product.products_by_location(id2location.keys(),
                    product_ids, with_childs=True)

            # Create a list of moves to create
            moves = {}
            for location in id2location.itervalues():
                for product_id in product_ids:
                    qty = pbl.get((location.id, product_id), 0)
                    op = product2op.get((location.id, product_id))
                    if op:
                        min_qty, max_qty = op.min_quantity, op.max_quantity
                        provisioning_location = op.provisioning_location
                    elif location and location.provisioning_location:
                        min_qty, max_qty = 0, 0
                        provisioning_location = location.provisioning_location
                    else:
                        continue
                    if qty < min_qty:
                        key = (
                            provisioning_location.id, location.id, product_id)
                        moves[key] = max_qty - qty

            # Group moves by {from,to}_location
            to_create = {}
            for key, qty in moves.iteritems():
                from_location, to_location, product = key
                to_create.setdefault(
                    (from_location, to_location), []).append((product, qty))
            # Create shipments and moves
            for locations, moves in to_create.iteritems():
                from_location, to_location = locations
                shipment = cls(
                    from_location=from_location,
                    to_location=to_location,
                    planned_date=date,
                    state='request',
                    )
                shipment_moves = []
                for move in moves:
                    product_id, qty = move
                    product = id2product.setdefault(
                        product_id, Product(product_id))
                    shipment_moves.append(Move(
                            from_location=from_location,
                            to_location=to_location,
                            planned_date=date,
                            product=product,
                            quantity=qty,
                            uom=product.default_uom,
                            company=user_record.company,
                            ))
                shipment.moves = shipment_moves
                shipment.planned_start_date = (
                    shipment.on_change_with_planned_start_date())
                shipments.append(shipment)
            date += datetime.timedelta(1)
        if shipments:
            cls.save(shipments)
            # Split moves through transit to get accurate dates
            cls._set_transit(shipments)
            cls.generate_internal_shipment(False)
            # Remove transit split as they are only requests
            Move.delete([m for s in shipments for m in s.moves
                    if m.from_location == s.transit_location])
        return shipments

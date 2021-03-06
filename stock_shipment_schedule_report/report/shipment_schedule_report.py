# -*- encoding: utf-8 -*-
from openerp.osv import fields, osv
from datetime import datetime
from dateutil.relativedelta import relativedelta
import time
from report import report_sxw
import logging
import pytz
from openerp import SUPERUSER_ID
from collections import Counter
from openerp.tools.translate import _

_logger = logging.getLogger(__name__)


class Parser(report_sxw.rml_parse):
    def __init__(self, cr, uid, name, context):
        super(Parser, self).__init__(cr, uid, name, context)
        self.localcontext.update( {
            'print_schedule':self.print_schedule,
        })

    def _get_user_tz(self, cr, uid, context=None):
        user = self.pool.get('res.users').browse(cr, SUPERUSER_ID, uid)
        if user.partner_id.tz:
            tz = pytz.timezone(user.partner_id.tz)
        else:
            tz = pytz.utc
        return tz
    
    def _get_header_info(self, cr, uid, dates, threshold_date, category_id, context=None):
        res = []
        if category_id:
            category = self.pool.get('product.category').browse(cr, uid, category_id).name
        else:
            category = 'ALL'
        header_vals = {
            'report_date': dates['current_date_local'],
            'threshold_date': threshold_date,
            'category': category,
            }
        res.append(header_vals)
        return res

    def _get_dates(self, cr, uid, tz, threshold_date, context=None):
        res = {}
        sys_date = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
        res['current_date_local'] = pytz.utc.localize(datetime.strptime(sys_date, '%Y-%m-%d %H:%M:%S')).astimezone(tz)
        res['current_date_utc'] = res['current_date_local'].astimezone(pytz.utc)
        threshold_date = datetime.strptime(threshold_date, '%Y-%m-%d')
        threshold_date_local = tz.localize(threshold_date, is_dst=None)
        res['threshold_date_utc'] = threshold_date_local.astimezone(pytz.utc)
        return res

    def _get_periods(self, cr, uid, dates, context=None):
        periods = {}
        current_date = dates['current_date_utc']
        threshold_date = dates['threshold_date_utc']
        i = 0
        for _ in xrange(7):
            if i == 0:  # for "Shipment on Hold" for the first period (period 2)
                start = current_date - relativedelta(years=100)
                end = current_date
            elif i == 1:  # for "Shipment" for the first period (period 2)
                start = current_date
                end = threshold_date + relativedelta(days=1)
            elif i == 2:
                start = threshold_date - relativedelta(years=100)
                end = threshold_date + relativedelta(days=1) 
            else:
                start = end
                if not i == 6:
                    end = start + relativedelta(days=7)
                else:  # for the last period
                    end += relativedelta(years=100)
            periods[i] = {
                'start': start,
                'end': end,
            }
            i += 1
        return periods

    def _get_line_title(self, cr, uid, periods, tz, context=None):
        line_title = []
        title_vals = {}
        for p in periods:
            if p >= 2:  # period 0 and 1 are ignored
                if p == 2:
                    start = ''
                else:
                    start =  datetime.strftime(periods[p]['start'].astimezone(tz), '%Y-%m-%d')
                if p == 6:
                    end = ''
                else:
                    end =  datetime.strftime(periods[p]['end'].astimezone(tz) - relativedelta(days=1), '%Y-%m-%d')
                title_vals['p'+`p`] = start + ' ~ ' + end
        line_title.append(title_vals)                
        return line_title

    def _get_product_ids(self, cr, uid, category_id, context=None):
        prod_ids = []
        prod_tmpl_ids = []
        prod_obj = self.pool.get('product.product')
        if category_id:  # identify all the categories under the selected category including itself
            categ_ids = [category_id]
            for categ in categ_ids:
                child_categ_ids = self.pool.get('product.category').search(cr, uid, [('parent_id','=',categ)])
                if child_categ_ids:
                    for child_categ in child_categ_ids:
                        categ_ids.append(child_categ)
            prod_ids = prod_obj.search(cr, uid, [('sale_ok','=',True),('type','=','product'),('categ_id','in',categ_ids),('active','=',True)])
        else:
            prod_ids = prod_obj.search(cr, uid, [('sale_ok','=',True),('type','=','product'),('active','=',True)])
        if prod_ids == []:
            raise osv.except_osv(_('Warning!'), _("There is no product to meet the condition (i.e. 'Saleable', 'Stockable Product' and belong to the selected Product Category or its offsprings)."))
        return prod_ids
 
    def _get_move_qty_data(self, cr, uid, params, context=None):
        res = {}
        sql = """
            select m.product_id, sum(m.product_qty / u.factor)
            from stock_move m
            left join product_uom u on (m.product_uom = u.id)
            where location_id %s %s
            and location_dest_id %s %s
            and product_id IN %s
            and state NOT IN ('done', 'cancel')
            and m.date >= %s
            and m.date < %s
            group by product_id
            """ % (tuple(params))
        cr.execute(sql)
        qty_dict = cr.dictfetchall()
        for rec in qty_dict:
            res[rec['product_id']] = rec['sum']
        return res
 
    def _get_quote_qty_data(self, cr, uid, params, context=None):
        res = {}
        sql = """
            select ol.product_id, sum(ol.product_uom_qty / u.factor)
            from sale_order_line ol
            left join product_uom u on (ol.product_uom = u.id)
            where product_id IN %s
            and order_id in
                (select id
                from sale_order
                where state = 'draft'
                and date_order >= '%s'
                and date_order < '%s')
            group by product_id
            """ % (tuple(params))
        cr.execute(sql)
        qty_dict = cr.dictfetchall()
        for rec in qty_dict:
            res[rec['product_id']] = rec['sum']
        return res
 
    def _get_dates4quote(self, cr, uid, period, context=None):
        res = {}
        usertz = self._get_user_tz(cr, uid, context=None)
        res['start'] = period['start'].astimezone(usertz).strftime('%Y-%m-%d')
        res['end'] = period['end'].astimezone(usertz).strftime('%Y-%m-%d')
        return res
    
    def _get_qty_data(self, cr, uid, product_ids, periods, line_vals, context=None):
        res = []
        param_prod_ids = str(product_ids).replace('[', '(').replace(']', ')')  # this conversion (instead of 'tuple(product_ids,)' is in case there is only one product)
        int_loc_ids = self.pool.get('stock.location').search(cr, uid, [('usage','=','internal')])
        i = 0
        for _ in xrange(7):
            date_from = "'"+str(periods[i]['start'])+"'"
            date_to = "'"+str(periods[i]['end'])+"'"
            in_params = ['NOT IN', tuple(int_loc_ids,), 'IN', tuple(int_loc_ids,), param_prod_ids, date_from, date_to]
            out_params = ['IN', tuple(int_loc_ids,), 'NOT IN', tuple(int_loc_ids,), param_prod_ids, date_from, date_to]
            dates4quote = self._get_dates4quote(cr, uid, periods[i], context=context)
            quote_params = [param_prod_ids, dates4quote['start'], dates4quote['end']]
            for what in ['in', 'out']:
                if what == 'in':
                    qty_data = self._get_move_qty_data(cr, uid, in_params, context=context)
                else:
                    move_qty_data = self._get_move_qty_data(cr, uid, out_params, context=context)
                    quote_qty_data = self._get_quote_qty_data(cr, uid, quote_params, context=context)
                    qty_data = dict(Counter(move_qty_data) + Counter(quote_qty_data))
                for k, v in qty_data.iteritems():
                    line_vals[k][what+`i`] = v
            i += 1
#         del_prod_list = []
        for prod in line_vals:  # identify products not to display (if no shipment, no display)
#             i = 0
#             del_flag = True
#             for _ in xrange(7):
#                 if line_vals[prod]['out'+`i`]:
#                     del_flag = False
#                 i += 1
#             if del_flag:
#                 del_prod_list.append(prod)
#             else:
#                 i = 2  # start from period "2" (period until threshold date)
#                 for _ in xrange(5):
#                     line_vals[prod]['bal'+`i`] = line_vals[prod]['bal'+`i-1`] + line_vals[prod]['in'+`i`] - line_vals[prod]['out'+`i`]
#                     i += 1
            i = 2  # start from period "2" (period until threshold date)
            for _ in xrange(5):
                line_vals[prod]['bal'+`i`] = line_vals[prod]['bal'+`i-1`] + line_vals[prod]['in'+`i`] - line_vals[prod]['out'+`i`]
                i += 1
#         for del_prod in del_prod_list:
#             del line_vals[del_prod]
        res.append(line_vals)
        return res
  
    def _get_lines(self, cr, uid, periods, category_id, context=None):
        res = []
        line_vals = {}
        product_ids = self._get_product_ids(cr, uid, category_id, context=None)
        prod_obj = self.pool.get('product.product')

        for prod in prod_obj.browse(cr, uid, product_ids):
            line_vals[prod.id] = {
                'name': prod.name,
                'categ': prod.categ_id.complete_name,
                'bal1': prod.qty_available,
                'in0': 0.0,
                'out0': 0.0,
                'in1': 0.0,
                'out1': 0.0,
                'in2': 0.0,  # first period in output
                'out2': 0.0,
                'bal2': 0.0,
                'in3': 0.0,  # second period in output
                'out3': 0.0,
                'bal3': 0.0,
                'in4': 0.0,  # third period in output
                'out4': 0.0,
                'bal4': 0.0,
                'in5': 0.0,  # fourth period in output
                'out5': 0.0,
                'bal5': 0.0,
                'in6': 0.0,  # fifth period in output
                'out6': 0.0,
                'bal6': 0.0,
            }
        lines = self._get_qty_data(cr, uid, product_ids, periods, line_vals, context=None)
        for line in lines:  # only append values (without key) to form the list
            for k, v in line.iteritems():
                res.append(v)
        res = sorted(res, key=lambda k: (k['categ'], k['name']))
        return res

    def print_schedule(self, data):
        res = []
        page = {}
        cr = self.cr
        uid = self.uid
        threshold_date = data['threshold_date'] or False
        category_id = data['category_id'] or False
        tz = self._get_user_tz(cr, uid, context=None)
        dates = self._get_dates(cr, uid, tz, threshold_date, context=None)
        periods = self._get_periods(cr, uid, dates, context=None)
        page['header'] = self._get_header_info(cr, uid, dates, threshold_date, category_id, context=None)
        page['line_title'] = self._get_line_title(cr, uid, periods, tz, context=None)
        page['lines'] = self._get_lines(cr, uid, periods, category_id, context=None)
        res.append(page)
        return res

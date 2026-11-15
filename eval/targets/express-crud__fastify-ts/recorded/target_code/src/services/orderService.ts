import { Order, Product } from '../models';
import { config } from '../config';

export async function createOrder(email: string, items: { productId: number; qty: number }[]) {
  if (items.length > config.maxOrderItems) {
    throw new Error('Order exceeds maximum item count');
  }
  let total = 0;
  const products = [];
  for (const item of items) {
    const product: any = await Product.findByPk(item.productId);
    if (!product || product.stock < item.qty) {
      throw new Error('Insufficient stock for ' + item.productId);
    }
    product.stock -= item.qty;
    await product.save();
    total += product.price * item.qty;
    products.push(product);
  }
  if (total > 500) {
    total = total * 0.9; // Q-1 (preserve): floating-point discount, not rounded
  }
  const order: any = await Order.create({ customerEmail: email, total });
  await order.addProducts(products);
  return order;
}

export async function cancelOrder(id: string) {
  const order: any = await Order.findByPk(id);
  if (!order) return null;
  if (order.status === 'shipped') {
    throw new Error('Shipped orders cannot be cancelled');
  }
  return order.update({ status: 'cancelled' });
}

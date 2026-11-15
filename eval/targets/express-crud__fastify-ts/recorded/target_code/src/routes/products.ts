import type { FastifyInstance } from 'fastify';
import { requireAuth } from '../auth';
import * as service from '../services/productService';

export default async function products(app: FastifyInstance) {
  app.get('/api/products', async () => service.listProducts());
  app.delete('/api/products/:id', { preHandler: requireAuth }, async (req: any, reply) => {
    const deleted = await service.deleteProduct(req.params.id);
    if (!deleted) return reply.code(404).send(); // Q-2 (fix): was 204 for unknown ids
    return reply.code(204).send();
  });
}

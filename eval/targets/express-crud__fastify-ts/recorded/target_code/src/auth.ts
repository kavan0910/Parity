import type { FastifyReply, FastifyRequest } from 'fastify';
import { config } from './config';

export async function requireAuth(req: FastifyRequest, reply: FastifyReply) {
  const presented = req.headers['x-api-token'];
  if (!presented || presented !== config.apiToken) {
    return reply.code(401).send({ error: 'unauthorized' });
  }
}

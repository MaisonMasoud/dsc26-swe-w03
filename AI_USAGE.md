
# AI usage

## Tools

I used ChatGPT to help me understand the task requirements, implement the FastAPI endpoints, create the SQLAlchemy models and Alembic migration, and debug errors.

I used VS Code for editing, pytest for testing, and Alembic for checking the database migration.

## What AI got wrong

ChatGPT initially provided an incomplete implementation of the transfer endpoint, which needed to be corrected.

It also used a normalized request body for idempotency comparison instead of preserving differences in the original JSON body.

I reviewed the README requirements and updated the implementation. I also fixed an indentation issue in main.py and reran the public tests, which passed.

## In my own words

1. Why does a transfer to someone else's account return 404 and not 403?

Returning 404 protects the privacy of other customers' accounts. A 403 response could reveal that the account exists, while 404 does not confirm its existence

2. What would happen to a customer if your idempotency record were written in a separate transaction after the transfer?

if the transfer succeeds but the system fails before saving the idempotency record, retrying the same request could execute the transfer again. This could cause the customer to be charged twice


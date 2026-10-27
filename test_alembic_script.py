import sqlalchemy as sa
c = sa.Column('dismissed', sa.Boolean, nullable=False)
c_copy = c.copy()
c_copy.server_default = sa.text('false')
print(c_copy.server_default)

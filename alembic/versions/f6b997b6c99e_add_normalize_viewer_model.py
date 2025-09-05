# add/normalize viewer model


op.alter_column(
    'wallet_transactions', 'type',
    existing_type=sa.VARCHAR(length=50),
    type_=wallet_txn_type,
    nullable=False,
    postgresql_using="type::wallet_txn_type",
)


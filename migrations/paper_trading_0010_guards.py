"""Version-pinned PostgreSQL guards and grants for migration 0010."""

from alembic import op

TRADING_TABLES = (
    "paper_account",
    "paper_account_event",
    "paper_cash_projection",
    "paper_position_projection",
    "portfolio_target",
    "risk_decision",
    "account_reservation",
    "reservation_event",
    "order_intent",
    "order_event",
    "paper_fill",
    "journal_transaction",
    "journal_posting",
    "paper_kill_switch",
)


def protect_trading_state() -> None:
    _protect_accounts()
    _protect_reservations()
    _protect_order_events()
    _protect_journal()
    op.execute(
        """
        create function validate_paper_kill_switch_mutation()
        returns trigger language plpgsql as $$ begin
            if tg_op = 'INSERT' then
                new.created_at := clock_timestamp(); new.updated_at := new.created_at;
                return new;
            end if;
            if new.switch_id <> old.switch_id or new.scope <> old.scope
               or new.account_id is distinct from old.account_id
               or new.created_at <> old.created_at or new.version <> old.version + 1 then
                raise exception 'kill switch identity/version is invalid' using errcode='40001';
            end if;
            new.updated_at := clock_timestamp(); return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_paper_kill_switch_mutation before insert or update "
        "on paper_kill_switch for each row execute function "
        "validate_paper_kill_switch_mutation()"
    )
    immutable = (
        "paper_account_event",
        "portfolio_target",
        "risk_decision",
        "reservation_event",
        "order_intent",
        "order_event",
        "paper_fill",
        "journal_transaction",
        "journal_posting",
    )
    for table in immutable:
        op.execute(
            f"create trigger trg_{table}_append_only before update or delete on {table} "
            "for each row execute function reject_append_only_mutation()"
        )
    for table in (
        "paper_account",
        "paper_cash_projection",
        "paper_position_projection",
        "account_reservation",
        "paper_kill_switch",
    ):
        op.execute(
            f"create trigger trg_{table}_no_delete before delete on {table} "
            "for each row execute function reject_append_only_mutation()"
        )


def _protect_accounts() -> None:
    op.execute(
        """
        create function validate_paper_account_mutation()
        returns trigger language plpgsql as $$ begin
            if tg_op = 'INSERT' then
                if new.aggregate_sequence <> 0 or new.portfolio_sequence <> 0
                   or new.ledger_sequence <> 0 or new.ledger_hash is not null then
                    raise exception 'paper account must start at genesis' using errcode='23514';
                end if;
                new.created_at := clock_timestamp(); new.updated_at := new.created_at;
                return new;
            end if;
            if new.account_id <> old.account_id or new.base_currency <> old.base_currency
               or new.created_at <> old.created_at
               or new.aggregate_sequence <> old.aggregate_sequence + 1
               or new.portfolio_sequence not in (old.portfolio_sequence, old.portfolio_sequence + 1)
               or new.ledger_sequence not in (old.ledger_sequence, old.ledger_sequence + 1)
               or (new.ledger_sequence = old.ledger_sequence and new.ledger_hash is distinct from old.ledger_hash)
               or (new.ledger_sequence = old.ledger_sequence + 1 and new.ledger_hash is null) then
                raise exception 'paper account CAS mutation is invalid' using errcode='40001';
            end if;
            new.updated_at := clock_timestamp(); return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_paper_account_mutation before insert or update on paper_account "
        "for each row execute function validate_paper_account_mutation()"
    )
    op.execute(
        """
        create function validate_paper_account_event_chain()
        returns trigger language plpgsql as $$
        declare prior paper_account_event%rowtype; current_sequence bigint; begin
            select aggregate_sequence into current_sequence from paper_account
            where account_id=new.account_id;
            if not found or new.sequence<>current_sequence then
                raise exception 'paper account event does not match account sequence'
                    using errcode='40001';
            end if;
            select * into prior from paper_account_event where account_id=new.account_id
            order by sequence desc limit 1 for update;
            if new.sequence=1 then
                if found or new.previous_hash is not null then
                    raise exception 'paper account genesis event is invalid' using errcode='23514';
                end if;
            elsif not found or new.sequence<>prior.sequence+1
                  or new.previous_hash<>prior.event_hash or new.occurred_at<prior.occurred_at then
                raise exception 'paper account event chain is invalid' using errcode='40001';
            end if; return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_paper_account_event_chain before insert on paper_account_event "
        "for each row execute function validate_paper_account_event_chain()"
    )
    op.execute(
        """
        create function require_paper_account_event()
        returns trigger language plpgsql as $$ begin
            if not exists(select 1 from paper_account_event e where e.account_id=new.account_id
              and e.sequence=new.aggregate_sequence and e.occurred_at=new.updated_at) then
                raise exception 'paper account mutation requires matching event' using errcode='23514';
            end if; return null;
        end $$
        """
    )
    op.execute(
        "create constraint trigger trg_paper_account_mutation_has_event after update on "
        "paper_account deferrable initially deferred for each row execute function "
        "require_paper_account_event()"
    )
    for name, table, immutable in (
        ("cash", "paper_cash_projection", "currency, quantum"),
        ("position", "paper_position_projection", "instrument_id, quantum"),
    ):
        op.execute(
            f"""
            create function validate_paper_{name}_projection()
            returns trigger language plpgsql as $$ declare current_sequence bigint; begin
                select aggregate_sequence into current_sequence from paper_account
                where account_id=new.account_id;
                if not found or new.updated_sequence<>current_sequence then
                    raise exception 'paper projection sequence is stale' using errcode='40001';
                end if;
                if tg_op='UPDATE' and (new.account_id<>old.account_id
                   or row(new.{immutable.split(", ")[0]}, new.{immutable.split(", ")[1]})
                   is distinct from row(old.{immutable.split(", ")[0]}, old.{immutable.split(", ")[1]})) then
                    raise exception 'paper projection identity is immutable' using errcode='55000';
                end if;
                new.updated_at:=clock_timestamp(); return new;
            end $$
            """
        )
        op.execute(
            f"create trigger trg_paper_{name}_projection before insert or update on {table} "
            f"for each row execute function validate_paper_{name}_projection()"
        )


def _protect_reservations() -> None:
    op.execute(
        """
        create function validate_account_reservation_projection()
        returns trigger language plpgsql as $$ begin
            if tg_op='INSERT' then
                if new.state<>'open' or new.event_sequence<>1 or new.previous_event_hash is not null
                   or new.remaining_amount<>new.amount then
                    raise exception 'reservation genesis is invalid' using errcode='23514';
                end if; return new;
            end if;
            if row(new.reservation_id,new.order_intent_id,new.account_id,new.instrument_id,
                   new.kind,new.commodity,new.amount,new.quantum,new.risk_decision_id,
                   new.risk_decision_hash,new.portfolio_target_id,new.authorized_target_hash,
                   new.risk_account_aggregate_sequence,new.account_aggregate_sequence,
                   new.portfolio_sequence,new.created_at,new.expires_at)
               is distinct from row(old.reservation_id,old.order_intent_id,old.account_id,
                   old.instrument_id,old.kind,old.commodity,old.amount,old.quantum,
                   old.risk_decision_id,old.risk_decision_hash,old.portfolio_target_id,
                   old.authorized_target_hash,old.risk_account_aggregate_sequence,
                   old.account_aggregate_sequence,old.portfolio_sequence,old.created_at,
                   old.expires_at)
               or new.event_sequence<>old.event_sequence+1
               or new.previous_event_hash<>old.event_hash
               or new.remaining_amount>old.remaining_amount then
                raise exception 'reservation projection mutation is invalid' using errcode='40001';
            end if;
            return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_account_reservation_projection before insert or update on "
        "account_reservation for each row execute function "
        "validate_account_reservation_projection()"
    )
    op.execute(
        """
        create function validate_reservation_event_chain()
        returns trigger language plpgsql as $$ declare prior reservation_event%rowtype; begin
            select * into prior from reservation_event where reservation_id=new.reservation_id
            order by sequence desc limit 1 for update;
            if new.sequence=1 then
                if found or new.previous_event_hash is not null or new.from_state is not null
                   or new.to_state<>'open' then
                    raise exception 'reservation genesis event is invalid' using errcode='23514';
                end if;
            elsif not found or new.sequence<>prior.sequence+1
                  or new.previous_event_hash<>prior.event_hash
                  or new.from_state<>prior.to_state or new.occurred_at<prior.occurred_at then
                raise exception 'reservation event chain is invalid' using errcode='40001';
            end if; return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_reservation_event_chain before insert on reservation_event "
        "for each row execute function validate_reservation_event_chain()"
    )
    op.execute(
        """
        create function require_reservation_event()
        returns trigger language plpgsql as $$ begin
            if not exists(select 1 from reservation_event e where e.reservation_id=new.reservation_id
              and e.sequence=new.event_sequence and e.event_hash=new.event_hash
              and e.previous_event_hash is not distinct from new.previous_event_hash
              and e.to_state=new.state and e.remaining_amount=new.remaining_amount
              and e.occurred_at=new.updated_at) then
                raise exception 'reservation mutation requires matching event' using errcode='23514';
            end if; return null;
        end $$
        """
    )
    op.execute(
        "create constraint trigger trg_reservation_mutation_has_event after insert or update "
        "on account_reservation deferrable initially deferred for each row execute function "
        "require_reservation_event()"
    )


def _protect_order_events() -> None:
    op.execute(
        """
        create function validate_order_event_chain()
        returns trigger language plpgsql as $$ declare prior order_event%rowtype; begin
            select * into prior from order_event where order_intent_id=new.order_intent_id
            order by sequence desc limit 1 for update;
            if new.sequence=1 then
                if found or new.previous_event_hash is not null or new.from_status<>'created' then
                    raise exception 'order genesis event is invalid' using errcode='23514';
                end if;
            elsif not found or new.sequence<>prior.sequence+1
                  or new.previous_event_hash<>prior.event_hash
                  or new.from_status<>prior.to_status
                  or new.cumulative_filled_quantity<prior.cumulative_filled_quantity
                  or new.occurred_at<prior.occurred_at then
                raise exception 'order event chain is invalid' using errcode='40001';
            end if;
            if not ((new.from_status='created' and new.to_status in ('accepted','rejected','cancelled','expired'))
              or (new.from_status='accepted' and new.to_status in ('partially_filled','filled','cancelled','expired'))
              or (new.from_status='partially_filled' and new.to_status in ('partially_filled','filled','cancelled','expired'))) then
                raise exception 'order state transition is invalid' using errcode='23514';
            end if; return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_order_event_chain before insert on order_event for each row "
        "execute function validate_order_event_chain()"
    )


def _protect_journal() -> None:
    op.execute(
        """
        create function validate_journal_transaction_chain()
        returns trigger language plpgsql as $$ declare prior journal_transaction%rowtype; begin
            select * into prior from journal_transaction where account_id=new.account_id
            order by sequence desc limit 1 for update;
            if new.sequence=1 then
                if found or new.previous_hash is not null then
                    raise exception 'journal genesis is invalid' using errcode='23514';
                end if;
            elsif not found or new.sequence<>prior.sequence+1 or new.previous_hash<>prior.transaction_hash
                  or new.occurred_at<prior.occurred_at then
                raise exception 'journal hash chain is invalid' using errcode='40001';
            end if; return new;
        end $$
        """
    )
    op.execute(
        "create trigger trg_journal_transaction_chain before insert on journal_transaction "
        "for each row execute function validate_journal_transaction_chain()"
    )
    op.execute(
        """
        create function require_balanced_journal()
        returns trigger language plpgsql as $$ declare target uuid; expected integer; begin
            target:=case when tg_table_name='journal_transaction' then new.transaction_id
                         else new.transaction_id end;
            select posting_count into expected from journal_transaction where transaction_id=target;
            if not found or (select count(*) from journal_posting where transaction_id=target)<>expected
               or exists(select 1 from journal_posting where transaction_id=target
                         group by commodity having count(distinct quantum)<>1
                         or sum(case when side='debit' then amount else -amount end)<>0) then
                raise exception 'journal transaction is not balanced and complete' using errcode='23514';
            end if; return null;
        end $$
        """
    )
    for trigger, table in (
        ("trg_journal_transaction_balanced", "journal_transaction"),
        ("trg_journal_posting_balanced", "journal_posting"),
    ):
        op.execute(
            f"create constraint trigger {trigger} after insert on {table} deferrable "
            "initially deferred for each row execute function require_balanced_journal()"
        )


def grant_trading_privileges() -> None:
    tables = ", ".join(TRADING_TABLES)
    op.execute(
        f"revoke all on {tables} from public, stonks_app, stonks_worker, stonks_reader"
    )
    op.execute(f"grant select on {tables} to stonks_reader")
    op.execute(f"grant select, insert on {tables} to stonks_app")
    updates = {
        "paper_account": "aggregate_sequence, portfolio_sequence, ledger_sequence, ledger_hash, updated_at",
        "paper_cash_projection": "settled_amount, reserved_amount, updated_sequence, updated_at",
        "paper_position_projection": "quantity, sellable_quantity, reserved_quantity, updated_sequence, updated_at",
        "account_reservation": "remaining_amount, state, updated_at, event_sequence, previous_event_hash, event_hash",
        "paper_kill_switch": "active, reason_code, actor, version, updated_at",
    }
    for table, columns in updates.items():
        op.execute(f"grant update ({columns}) on {table} to stonks_app")

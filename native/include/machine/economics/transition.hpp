#pragma once

#include "machine/economics/decision.hpp"

namespace machine::economics {

enum class EconomicPhase : std::uint32_t {
    empty,
    observed,
    proposed,
    simulated,
    awaiting_authorization,
    authorized,
    transmitting,
    unknown,
    included,
    finalized,
    verified,
    settled,
    aborted,
    escalated,
};

enum class TransitionEvent : std::uint32_t {
    observe,
    propose,
    simulate,
    request_authorization,
    authorize,
    transmit,
    ambiguous,
    include,
    finalize,
    verify_post_state,
    settle,
    withdraw_unsent,
    escalate,
};

struct EconomicIdentity {
    std::uint64_t process{};
    std::uint64_t program{};
    std::uint64_t program_version{};
    std::uint64_t policy{};
    std::uint64_t policy_version{};
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t asset{};
};

[[nodiscard]] inline bool same_identity(
    const EconomicIdentity& a,
    const EconomicIdentity& b
) noexcept {
    return a.process == b.process && a.program == b.program && a.program_version == b.program_version &&
        a.policy == b.policy && a.policy_version == b.policy_version && a.account == b.account &&
        a.network == b.network && a.asset == b.asset;
}

[[nodiscard]] inline bool valid_identity(const EconomicIdentity& identity) noexcept {
    return identity.process && identity.program && identity.program_version && identity.policy &&
        identity.policy_version && identity.account && identity.network && identity.asset;
}

struct CapitalInvariant {
    std::int64_t ceiling{};
    std::int64_t reserved{};
    std::int64_t spent{};
    std::int64_t max_single{};
    std::int64_t max_fee{};
    std::int64_t max_loss{};
    std::int64_t exposure_limit{};
    std::int64_t immediate_cash_floor{};
};

struct ActionEnvelope {
    EconomicAction action{};
    std::uint64_t intent{};
    std::uint64_t plan_commitment{};
    std::uint64_t state_commitment{};
    std::uint64_t destination{};
    std::uint64_t expires_ns{};
    std::int64_t amount{};
    std::int64_t maximum_cost{};
    std::int64_t maximum_loss{};
    std::int64_t exposure_after{};
    std::int64_t immediate_cash_after{};
    bool destination_allowed{};
};

struct SimulationEvidence {
    std::uint64_t intent{};
    std::uint64_t plan_commitment{};
    std::uint64_t state_commitment{};
    std::uint64_t evidence_commitment{};
    std::uint64_t observed_ns{};
    std::int64_t projected_amount{};
    std::int64_t projected_cost{};
    std::int64_t projected_loss{};
    std::int64_t projected_exposure{};
    std::int64_t projected_cash{};
    bool adapter_verified{};
    bool successful{};
};

struct AuthorizationEvidence {
    std::uint64_t authorization{};
    std::uint64_t intent{};
    std::uint64_t plan_commitment{};
    std::uint64_t account{};
    std::uint64_t network{};
    std::uint64_t policy_version{};
    std::uint64_t authorized_ns{};
    std::uint64_t expires_ns{};
    bool externally_verified{};
};

struct SettlementEvidence {
    std::uint64_t intent{};
    std::uint64_t transaction_commitment{};
    std::uint64_t block_commitment{};
    std::uint64_t block_height{};
    std::uint64_t finalized_height{};
    std::uint64_t source_a{};
    std::uint64_t source_b{};
    std::uint64_t post_state_commitment{};
    std::int64_t actual_amount{};
    std::int64_t actual_cost{};
    std::int64_t actual_loss{};
    std::int64_t actual_exposure{};
    std::int64_t actual_cash{};
    bool successful{};
    bool independent_sources_agree{};
    bool adapter_verified{};
};

struct TransitionInput {
    std::uint64_t event{};
    std::uint64_t fingerprint{};
    TransitionEvent kind{};
    EconomicIdentity identity{};
    Envelope state{};
    ActionEnvelope action{};
    SimulationEvidence simulation{};
    AuthorizationEvidence authorization{};
    SettlementEvidence settlement{};
};

struct EconomicProcess {
    EconomicIdentity identity{};
    EconomicPhase phase{EconomicPhase::empty};
    CapitalInvariant capital{};
    ActionEnvelope action{};
    SimulationEvidence simulation{};
    AuthorizationEvidence authorization{};
    SettlementEvidence settlement{};
    std::uint64_t sequence{};
    std::uint64_t last_event{};
    std::uint64_t last_fingerprint{};
    std::uint64_t last_observed_ns{};
    std::int64_t intent_reserved{};
    bool policy_paused{};
    bool needs_resolver{};
};

struct TransitionReceipt {
    Error error{Error::unknown};
    EconomicPhase before{};
    EconomicPhase after{};
    std::uint64_t event{};
    std::uint64_t sequence{};
    std::int64_t capital_held{};
    std::int64_t capital_spent{};
    std::uint64_t diagnostic_fingerprint{};
    bool replay{};
    std::uint64_t execution_authority{};
};

[[nodiscard]] inline Error validate_capital(const CapitalInvariant& capital) noexcept {
    if (capital.ceiling < 0 || capital.reserved < 0 || capital.spent < 0 || capital.max_single < 0 ||
        capital.max_single > capital.ceiling || capital.max_fee < 0 || capital.max_loss < 0 ||
        capital.exposure_limit < 0 || capital.immediate_cash_floor < 0 || capital.spent > capital.ceiling ||
        capital.reserved > capital.ceiling - capital.spent) {
        return Error::invalid;
    }
    return Error::okay;
}

[[nodiscard]] inline Error check_action(
    const EconomicProcess& process,
    const ActionEnvelope& action,
    std::uint64_t now_ns
) noexcept {
    if (!action.intent || !action.plan_commitment || !action.state_commitment || !action.destination ||
        action.amount <= 0 || action.maximum_cost < 0 || action.maximum_loss < 0 ||
        action.exposure_after < 0 || action.immediate_cash_after < 0 ||
        action.action == EconomicAction::none || action.action == EconomicAction::hold ||
        action.action == EconomicAction::escalate || action.action == EconomicAction::abort ||
        static_cast<std::uint32_t>(action.action) > static_cast<std::uint32_t>(EconomicAction::abort)) {
        return Error::invalid;
    }
    if (!action.destination_allowed || process.policy_paused) {
        return Error::unauthorized;
    }
    if (now_ns >= action.expires_ns) {
        return Error::expired;
    }
    if (action.amount > process.capital.max_single || action.maximum_cost > process.capital.max_fee ||
        action.maximum_loss > process.capital.max_loss || action.exposure_after > process.capital.exposure_limit ||
        action.immediate_cash_after < process.capital.immediate_cash_floor) {
        return Error::infeasible;
    }
    const auto encumbrance = add(action.amount, action.maximum_cost);
    if (!encumbrance) {
        return encumbrance.error;
    }
    if (encumbrance.value > process.capital.ceiling - process.capital.spent - process.capital.reserved) {
        return Error::cost;
    }
    return Error::okay;
}

[[nodiscard]] inline Error check_simulation(
    const EconomicProcess& process,
    const SimulationEvidence& simulation,
    const Envelope& state
) noexcept {
    const auto& action = process.action;
    if (simulation.intent != action.intent || simulation.plan_commitment != action.plan_commitment ||
        simulation.state_commitment != action.state_commitment || !simulation.evidence_commitment ||
        !simulation.adapter_verified || !simulation.successful || simulation.observed_ns > state.now_ns ||
        state.now_ns - simulation.observed_ns > state.max_age_ns) {
        return Error::invalid;
    }
    if (simulation.projected_amount < 0 || simulation.projected_amount > action.amount ||
        simulation.projected_cost < 0 || simulation.projected_cost > action.maximum_cost ||
        simulation.projected_loss < 0 || simulation.projected_loss > action.maximum_loss ||
        simulation.projected_exposure < 0 || simulation.projected_exposure > process.capital.exposure_limit ||
        simulation.projected_cash < process.capital.immediate_cash_floor) {
        return Error::infeasible;
    }
    return Error::okay;
}

[[nodiscard]] inline Error check_authorization(
    const EconomicProcess& process,
    const AuthorizationEvidence& authorization,
    std::uint64_t now_ns
) noexcept {
    if (!authorization.authorization || authorization.intent != process.action.intent ||
        authorization.plan_commitment != process.action.plan_commitment ||
        authorization.account != process.identity.account || authorization.network != process.identity.network ||
        authorization.policy_version != process.identity.policy_version || !authorization.externally_verified ||
        authorization.authorized_ns > now_ns || authorization.expires_ns <= now_ns ||
        authorization.expires_ns > process.action.expires_ns) {
        return Error::unauthorized;
    }
    if (process.policy_paused) {
        return Error::unauthorized;
    }
    return Error::okay;
}

[[nodiscard]] inline Error check_settlement(
    const EconomicProcess& process,
    const SettlementEvidence& evidence,
    bool require_finality,
    bool require_post_state
) noexcept {
    if (evidence.intent != process.action.intent || !evidence.transaction_commitment ||
        !evidence.block_commitment || !evidence.block_height || !evidence.source_a || !evidence.source_b ||
        evidence.source_a == evidence.source_b || !evidence.successful ||
        !evidence.independent_sources_agree || !evidence.adapter_verified || evidence.actual_amount < 0 ||
        evidence.actual_cost < 0 || evidence.actual_loss < 0 || evidence.actual_exposure < 0 || evidence.actual_cash < 0) {
        return Error::invalid;
    }
    if (process.settlement.transaction_commitment &&
        process.settlement.transaction_commitment != evidence.transaction_commitment) {
        return Error::conflict;
    }
    if ((process.phase == EconomicPhase::included || process.phase == EconomicPhase::finalized ||
         process.phase == EconomicPhase::verified) &&
        (process.settlement.block_commitment != evidence.block_commitment ||
         process.settlement.block_height != evidence.block_height)) {
        return Error::reorg;
    }
    if (require_finality && evidence.finalized_height < evidence.block_height) {
        return Error::incomplete;
    }
    if (require_post_state && (!evidence.post_state_commitment || evidence.actual_amount > process.action.amount ||
        evidence.actual_cost > process.action.maximum_cost || evidence.actual_loss > process.action.maximum_loss ||
        evidence.actual_exposure > process.capital.exposure_limit ||
        evidence.actual_cash < process.capital.immediate_cash_floor)) {
        return Error::infeasible;
    }
    return Error::okay;
}

[[nodiscard]] inline std::uint64_t transition_fingerprint(const TransitionInput& input) noexcept {
    Fingerprint fingerprint;
    const auto append = [&](auto value) { fingerprint.append(static_cast<std::uint64_t>(value)); };
    append(input.event);
    append(input.fingerprint);
    append(input.kind);
    for (auto value : {input.identity.process, input.identity.program, input.identity.program_version,
        input.identity.policy, input.identity.policy_version, input.identity.account, input.identity.network,
        input.identity.asset, input.state.sequence, input.state.expected_sequence, input.state.observed_ns,
        input.state.now_ns, input.state.max_age_ns, input.state.deadline_ns, input.state.policy_version,
        input.state.expected_policy_version}) append(value);
    append(input.action.action);
    for (auto value : {input.action.intent, input.action.plan_commitment, input.action.state_commitment,
        input.action.destination, input.action.expires_ns}) append(value);
    for (auto value : {input.action.amount, input.action.maximum_cost, input.action.maximum_loss,
        input.action.exposure_after, input.action.immediate_cash_after}) append(value);
    append(input.action.destination_allowed);
    for (auto value : {input.simulation.intent, input.simulation.plan_commitment, input.simulation.state_commitment,
        input.simulation.evidence_commitment, input.simulation.observed_ns}) append(value);
    for (auto value : {input.simulation.projected_amount, input.simulation.projected_cost,
        input.simulation.projected_loss, input.simulation.projected_exposure, input.simulation.projected_cash}) append(value);
    append(input.simulation.adapter_verified);
    append(input.simulation.successful);
    for (auto value : {input.authorization.authorization, input.authorization.intent,
        input.authorization.plan_commitment, input.authorization.account, input.authorization.network,
        input.authorization.policy_version, input.authorization.authorized_ns, input.authorization.expires_ns}) append(value);
    append(input.authorization.externally_verified);
    for (auto value : {input.settlement.intent, input.settlement.transaction_commitment,
        input.settlement.block_commitment, input.settlement.block_height, input.settlement.finalized_height,
        input.settlement.source_a, input.settlement.source_b, input.settlement.post_state_commitment}) append(value);
    for (auto value : {input.settlement.actual_amount, input.settlement.actual_cost, input.settlement.actual_loss,
        input.settlement.actual_exposure, input.settlement.actual_cash}) append(value);
    append(input.settlement.successful);
    append(input.settlement.independent_sources_agree);
    append(input.settlement.adapter_verified);
    return fingerprint.value();
}

// Single-writer mirror. The authority layer persists each input/receipt before
// external effects, verifies actual signatures/chain state, and restores this
// mirror by replay. Diagnostic 64-bit IDs must never replace SHA-256 receipts
// or the external verifier. There is no signing or transmission callback here.
[[nodiscard]] inline TransitionReceipt advance_process(
    EconomicProcess& process,
    const TransitionInput& input
) noexcept {
    TransitionReceipt receipt{};
    receipt.before = process.phase;
    receipt.after = process.phase;
    receipt.event = input.event;
    receipt.sequence = process.sequence;
    receipt.capital_held = process.capital.reserved;
    receipt.capital_spent = process.capital.spent;
    const auto reject = [&](Error error) {
        receipt.error = error;
        return receipt;
    };
    if (!input.event || !input.fingerprint || !valid_identity(input.identity) ||
        !same_identity(process.identity, input.identity) || validate_capital(process.capital) != Error::okay) {
        return reject(Error::invalid);
    }
    if (process.last_event == input.event) {
        if (process.last_fingerprint != transition_fingerprint(input)) {
            return reject(Error::conflict);
        }
        receipt.error = Error::okay;
        receipt.replay = true;
        return receipt;
    }
    if (input.event < process.last_event || process.sequence == UINT64_MAX) {
        return reject(Error::sequence);
    }
    if (process.phase == EconomicPhase::settled || process.phase == EconomicPhase::aborted ||
        process.phase == EconomicPhase::escalated) {
        return reject(Error::conflict);
    }
    auto next = process;
    Error result = Error::okay;
    switch (input.kind) {
    case TransitionEvent::observe:
        if (process.phase != EconomicPhase::empty || validate(input.state) != Error::okay ||
            input.state.policy_version != process.identity.policy_version) {
            return reject(Error::invalid);
        }
        next.phase = EconomicPhase::observed;
        next.last_observed_ns = input.state.observed_ns;
        break;
    case TransitionEvent::propose:
        if (process.phase != EconomicPhase::observed || validate(input.state) != Error::okay ||
            input.state.policy_version != process.identity.policy_version ||
            input.state.observed_ns < process.last_observed_ns) {
            return reject(Error::invalid);
        }
        result = check_action(process, input.action, input.state.now_ns);
        if (result != Error::okay) {
            return reject(result);
        }
        next.action = input.action;
        next.phase = EconomicPhase::proposed;
        break;
    case TransitionEvent::simulate:
        if (process.phase != EconomicPhase::proposed || validate(input.state) != Error::okay) {
            return reject(Error::invalid);
        }
        result = check_simulation(process, input.simulation, input.state);
        if (result != Error::okay) {
            return reject(result);
        }
        next.simulation = input.simulation;
        next.phase = EconomicPhase::simulated;
        break;
    case TransitionEvent::request_authorization: {
        if (process.phase != EconomicPhase::simulated || validate(input.state) != Error::okay) {
            return reject(Error::invalid);
        }
        result = check_action(process, process.action, input.state.now_ns);
        if (result != Error::okay) {
            return reject(result);
        }
        const auto amount = add(process.action.amount, process.action.maximum_cost);
        if (!amount) {
            return reject(amount.error);
        }
        next.capital.reserved += amount.value;
        next.intent_reserved = amount.value;
        next.phase = EconomicPhase::awaiting_authorization;
        break;
    }
    case TransitionEvent::authorize:
        if (process.phase != EconomicPhase::awaiting_authorization || validate(input.state) != Error::okay) {
            return reject(Error::invalid);
        }
        result = check_authorization(process, input.authorization, input.state.now_ns);
        if (result != Error::okay) {
            return reject(result);
        }
        next.authorization = input.authorization;
        next.phase = EconomicPhase::authorized;
        break;
    case TransitionEvent::transmit:
        if (process.phase != EconomicPhase::authorized || validate(input.state) != Error::okay) {
            return reject(Error::invalid);
        }
        result = check_authorization(process, process.authorization, input.state.now_ns);
        if (result != Error::okay) {
            return reject(result);
        }
        next.phase = EconomicPhase::transmitting;
        break;
    case TransitionEvent::ambiguous:
        if (process.phase != EconomicPhase::transmitting && process.phase != EconomicPhase::included &&
            process.phase != EconomicPhase::finalized) {
            return reject(Error::invalid);
        }
        next.phase = EconomicPhase::unknown;
        break;
    case TransitionEvent::include:
        if (process.phase != EconomicPhase::transmitting && process.phase != EconomicPhase::unknown) {
            return reject(Error::invalid);
        }
        result = check_settlement(process, input.settlement, false, false);
        if (result != Error::okay) {
            return reject(result);
        }
        next.settlement = input.settlement;
        next.phase = EconomicPhase::included;
        break;
    case TransitionEvent::finalize:
        if (process.phase != EconomicPhase::included && process.phase != EconomicPhase::unknown) {
            return reject(Error::invalid);
        }
        result = check_settlement(process, input.settlement, true, false);
        if (result != Error::okay) {
            return reject(result);
        }
        next.settlement = input.settlement;
        next.phase = EconomicPhase::finalized;
        break;
    case TransitionEvent::verify_post_state:
        if (process.phase != EconomicPhase::finalized) {
            return reject(Error::invalid);
        }
        result = check_settlement(process, input.settlement, true, true);
        if (result != Error::okay) {
            return reject(result);
        }
        next.settlement = input.settlement;
        next.phase = EconomicPhase::verified;
        break;
    case TransitionEvent::settle: {
        if (process.phase != EconomicPhase::verified || process.intent_reserved <= 0 ||
            process.capital.reserved < process.intent_reserved) {
            return reject(Error::invalid);
        }
        const auto charged = add(process.settlement.actual_amount, process.settlement.actual_cost);
        if (!charged) {
            return reject(charged.error);
        }
        if (charged.value > process.intent_reserved || charged.value > process.capital.ceiling - process.capital.spent) {
            return reject(Error::cost);
        }
        next.capital.reserved -= process.intent_reserved;
        next.capital.spent += charged.value;
        next.intent_reserved = 0;
        next.phase = EconomicPhase::settled;
        break;
    }
    case TransitionEvent::withdraw_unsent:
        if (process.phase == EconomicPhase::transmitting || process.phase == EconomicPhase::unknown ||
            process.phase == EconomicPhase::included || process.phase == EconomicPhase::finalized ||
            process.phase == EconomicPhase::verified) {
            return reject(Error::unauthorized);
        }
        if (process.capital.reserved < process.intent_reserved) {
            return reject(Error::invalid);
        }
        next.capital.reserved -= process.intent_reserved;
        next.intent_reserved = 0;
        next.phase = EconomicPhase::aborted;
        break;
    case TransitionEvent::escalate:
        // A resolver request must not turn a pending payment into an
        // unrecoverable terminal state or release its uncertain encumbrance.
        next.needs_resolver = true;
        if (process.phase == EconomicPhase::transmitting || process.phase == EconomicPhase::unknown ||
            process.phase == EconomicPhase::included || process.phase == EconomicPhase::finalized ||
            process.phase == EconomicPhase::verified) {
            next.phase = EconomicPhase::unknown;
        } else if (!process.intent_reserved) {
            next.phase = EconomicPhase::escalated;
        }
        break;
    default:
        return reject(Error::unsupported);
    }
    next.last_event = input.event;
    next.last_fingerprint = transition_fingerprint(input);
    ++next.sequence;
    Fingerprint diagnostic;
    diagnostic.append(next.identity.process);
    diagnostic.append(next.identity.program_version);
    diagnostic.append(next.identity.policy_version);
    diagnostic.append(next.sequence);
    diagnostic.append(input.event);
    diagnostic.append(input.fingerprint);
    diagnostic.append(static_cast<std::uint32_t>(process.phase));
    diagnostic.append(static_cast<std::uint32_t>(next.phase));
    diagnostic.append(static_cast<std::uint64_t>(next.capital.reserved));
    diagnostic.append(static_cast<std::uint64_t>(next.capital.spent));
    process = next;
    receipt.error = Error::okay;
    receipt.after = next.phase;
    receipt.sequence = next.sequence;
    receipt.capital_held = next.capital.reserved;
    receipt.capital_spent = next.capital.spent;
    receipt.diagnostic_fingerprint = diagnostic.value();
    return receipt;
}

} // namespace machine::economics

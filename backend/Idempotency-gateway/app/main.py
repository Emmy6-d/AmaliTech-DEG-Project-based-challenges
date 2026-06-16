from fastapi import FastAPI, Request, Response, HTTPException, status
from app.models import PaymentRequest
from app.storage import InMemoryIdempotencyStore, generate_fingerprint
import asyncio

app = FastAPI(
    title="Idempotency Gateway (The Pay-Once Protocol)",
    description="A robust payment processor middleware simulating transactional safety and race-condition prevention.",
    version="1.0.0"
)

# Instantiate the global in-memory idempotency store.
# Default TTL is set to 60.0 seconds as specified by the Developer's Choice challenge.
store = InMemoryIdempotencyStore(default_ttl_seconds=60.0)

@app.post("/process-payment")
async def process_payment(
    request: Request,
    payload: PaymentRequest,
    response: Response
):
    """
    Process a payment with idempotency safety checks.
    
    1. Validates that Idempotency-Key is present.
    2. Performs SHA-256 fingerprinting on the request payload.
    3. Handles in-flight requests concurrently using asyncio.Future.
    4. Replays completed payments instantly without processing.
    5. Rejects keys reused with different payloads (HTTP 409 Conflict).
    6. Respects TTL key expiration.
    """
    idempotency_key = request.headers.get("Idempotency-Key")
    
    # Missing Idempotency-Key: HTTP 400 Bad Request
    if not idempotency_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key header is required"
        )
    
    # Generate request body fingerprint
    body_dict = payload.model_dump()
    request_hash = generate_fingerprint(body_dict)
    
    # Check if we have an existing record (takes care of lazy TTL cleanup)
    record = store.get_record(idempotency_key)
    
    if record:
        # Case A: Fraud / Data Integrity check
        # Reuse of key with a different payload
        if record.request_hash != request_hash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Idempotency key already used for a different request body."
            )
            
        # Case B: Concurrent request (In-flight checking)
        if record.is_processing:
            try:
                # Wait for the first request to complete processing
                status_code, cached_response = await record.future
                
                # Setup response properties for the waiting client
                response.status_code = status_code
                response.headers["X-Cache-Hit"] = "true"
                return cached_response
            except Exception as e:
                # If the first request errored out, we raise 500 so this request is not stuck
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"In-flight payment processing failed: {str(e)}"
                )
                
        # Case C: Duplicate completed request replay
        response.status_code = record.status_code
        response.headers["X-Cache-Hit"] = "true"
        return record.response_body

    # Case D: New request execution
    record = store.create_record(idempotency_key, request_hash)
    
    try:
        # Simulate payment processing latency (2 seconds)
        await asyncio.sleep(2.0)
        
        # Prepare response body format (GHS currency format)
        amount_str = str(int(payload.amount)) if payload.amount.is_integer() else str(payload.amount)
        response_body = {"message": f"Charged {amount_str} {payload.currency}"}
        status_code = status.HTTP_200_OK
        
        # Save completion status in the record
        record.status_code = status_code
        record.response_body = response_body
        record.is_processing = False
        record.reset_timestamp()
        
        # Resolve the Future for any waiting concurrent clients
        record.future.set_result((status_code, response_body))
        
        # Return response to the first client
        response.status_code = status_code
        response.headers["X-Cache-Hit"] = "false"
        return response_body
        
    except Exception as e:
        # Clean up record so that clients can retry
        store.delete_record(idempotency_key)
        
        # Set exception on future to notify concurrent waiters
        if not record.future.done():
            record.future.set_exception(e)
            
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment processing failed: {str(e)}"
        )

"""LoRA fine-tuning scaffold for Qwen-8B (ships unexecuted; user runs on request)."""

"""
This scaffold provides the structure for fine-tuning Qwen-8B using PEFT (Parameter-Efficient Fine-Tuning).
To execute:

1. Install PEFT dependencies:
   pip install peft

2. Prepare training data:
   - Load financial instruction dataset (e.g., from RAG corpus + synthetic instructions)
   - Format as conversation pairs

3. Run training:
   python -c "from app.foundation.finagent.lora_scaffold import train_lora_qwen; train_lora_qwen(...)"

See Theerthala et al. 2025 "Behaviorally Grounded Reasoning" for fine-tuning strategy.
"""

# Scaffold (not executed)
def train_lora_qwen(
    output_dir: str = "/models/qwen-8b-lora",
    training_samples: list[dict] | None = None,
) -> None:
    """Fine-tune Qwen-8B with LoRA on financial instruction data.

    Args:
        output_dir: Where to save the LoRA weights
        training_samples: List of {"instruction": ..., "input": ..., "output": ...} dicts
    """
    try:
        peft = __import__("peft")
        transformers = __import__("transformers")
        LoraConfig = peft.LoraConfig
        get_peft_model = peft.get_peft_model
        AutoModelForCausalLM = transformers.AutoModelForCausalLM
        AutoTokenizer = transformers.AutoTokenizer
        TrainingArguments = transformers.TrainingArguments
    except ImportError:
        raise ImportError("peft and transformers required: pip install peft transformers")

    # NOTE: incomplete experimental LoRA scaffold — Trainer/tokenizer/training_args are kept for
    # the (commented-out) Trainer wiring below. Tracked in docs/archive/TODO-experimental.md.
    # Load base model
    model_id = "Qwen/Qwen-8B"
    tokenizer = AutoTokenizer.from_pretrained(model_id)  # noqa: F841
    model = AutoModelForCausalLM.from_pretrained(model_id, load_in_8bit=True)

    # Configure LoRA
    lora_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "v_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)

    # Prepare training data
    if training_samples is None:
        training_samples = []

    # Format for training
    def format_instruction(sample: dict) -> str:
        return f"""### Instruction:
{sample.get('instruction', '')}

### Input:
{sample.get('input', '')}

### Response:
{sample.get('output', '')}"""

    # Training configuration
    training_args = TrainingArguments(  # noqa: F841
        output_dir=output_dir,
        per_device_train_batch_size=4,
        per_device_eval_batch_size=4,
        num_train_epochs=3,
        save_steps=500,
        save_total_limit=3,
        logging_steps=100,
        learning_rate=2e-4,
    )

    # Initialize trainer (data preparation would happen here)
    # trainer = Trainer(
    #     model=model,
    #     args=training_args,
    #     train_dataset=train_dataset,
    #     tokenizer=tokenizer,
    # )
    # trainer.train()

    print("LoRA training scaffold ready. Call train_lora_qwen() with training_samples to execute.")
